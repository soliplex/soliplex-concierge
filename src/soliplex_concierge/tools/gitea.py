"""Tools supporting the Gitea integration used by the 'about_soliplex' room.

Issue creation talks to the Gitea REST API directly (no MCP server): the
repository is fixed by the room's tool configuration, so the agent cannot
file against an arbitrary repository.
"""

import httpx
import pydantic
import pydantic_ai
import yaml
from soliplex import agents
from soliplex import models

from soliplex_concierge import config
from soliplex_concierge import tls
from soliplex_concierge.labels import ISSUE_TYPE_LABELS

# Filename used for the requesting user's profile, attached to every issue.
PROFILE_ATTACHMENT_NAME = "user-profile.yaml"


class UnknownRequestType(ValueError):
    """A 'request_type' with no matching issue-type label was supplied."""

    def __init__(self, request_type: str, supported: set[str]):
        self.request_type = request_type
        self.supported = supported
        super().__init__(
            f"unknown request_type {request_type!r}; "
            f"expected one of {sorted(supported)}"
        )


class AnonymousUser(ValueError):
    """No user profile is attached to the request; we cannot attribute it."""

    def __init__(self):
        super().__init__(
            "cannot file a room request for an anonymous user "
            "(no user profile is attached to this run)"
        )


class CreatedGiteaIssue(pydantic.BaseModel):
    """A Gitea issue created on behalf of a room request."""

    number: int
    url: str
    title: str


async def _ensure_label(
    client: httpx.AsyncClient,
    repo_url: str,
    headers: dict[str, str],
    name: str,
) -> int:
    """Return the id of the named repo label, creating it if it is absent."""
    response = await client.get(f"{repo_url}/labels", headers=headers)
    response.raise_for_status()
    for label in response.json():
        if label["name"] == name:
            return label["id"]

    spec = ISSUE_TYPE_LABELS[name]
    response = await client.post(
        f"{repo_url}/labels",
        headers=headers,
        json={
            "name": name,
            "color": spec["color"],
            "description": spec["description"],
        },
    )
    response.raise_for_status()
    return response.json()["id"]


async def _attach_user_profile(
    client: httpx.AsyncClient,
    repo_url: str,
    headers: dict[str, str],
    number: int,
    user: models.UserProfile,
) -> None:
    """Attach the requesting user's profile to the issue as a YAML file."""
    content = yaml.safe_dump(user.model_dump(), sort_keys=False)
    response = await client.post(
        f"{repo_url}/issues/{number}/assets",
        headers=headers,
        params={"name": PROFILE_ATTACHMENT_NAME},
        files={
            "attachment": (
                PROFILE_ATTACHMENT_NAME,
                content.encode("utf-8"),
                "application/x-yaml",
            )
        },
    )
    response.raise_for_status()


async def create_gitea_issue(
    ctx: pydantic_ai.RunContext[agents.AgentDependencies],
    title: str,
    body: str,
    request_type: str,
) -> CreatedGiteaIssue:
    """File a room request as a Gitea issue and return its number and link.

    The target repository is fixed by this room's configuration, so you do
    not choose it -- just supply a clear, specific 'title' and a 'body' that
    captures the request type, the requesting user, and every detail you
    collected. The issue is tagged with a type label so administrators can
    triage it; the label is created on the repository if it does not yet
    exist. The requesting user's profile is attached to the issue as a YAML
    file ('user-profile.yaml'), so you need not transcribe their identity into
    the 'body'. Report the returned issue number and URL back to the user.

    Args:
        title: the issue title.
        body: the issue body (Markdown).
        request_type: which kind of request this is -- 'new-room' for a new
            room, or 'room-access' for access to an existing private room
            (the 'TYPE:' line returned by the room-request concierge skill).

    Returns:
        CreatedGiteaIssue: the 'number', 'url', and 'title' of the new issue.

    Raises:
        UnknownRequestType: if 'request_type' is not a known issue-type label.
        AnonymousUser: if no user profile is attached to the run.
    """
    if request_type not in ISSUE_TYPE_LABELS:
        raise UnknownRequestType(request_type, set(ISSUE_TYPE_LABELS))

    if ctx.deps.user is None:
        raise AnonymousUser()

    tool_config = ctx.deps.tool_configs[config.CGI_TOOL_KIND]

    host = tool_config.host
    token = tool_config.token
    repo_url = (
        f"{host.rstrip('/')}"
        f"/api/v1/repos/{tool_config.owner}/{tool_config.repo}"
    )
    headers = {"Authorization": f"token {token}"}

    async with httpx.AsyncClient(verify=tls.httpx_verify()) as client:
        label_id = await _ensure_label(client, repo_url, headers, request_type)
        response = await client.post(
            f"{repo_url}/issues",
            headers=headers,
            json={"title": title, "body": body, "labels": [label_id]},
        )
        response.raise_for_status()
        data = response.json()

        await _attach_user_profile(
            client, repo_url, headers, data["number"], ctx.deps.user
        )

    return CreatedGiteaIssue(
        number=data["number"],
        url=data["html_url"],
        title=data["title"],
    )
