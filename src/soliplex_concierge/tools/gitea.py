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
from soliplex_concierge.labels import LABEL_PAGE_SIZE
from soliplex_concierge.labels import MAX_LABEL_PAGES


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


class LabelNotApplied(RuntimeError):
    """The issue was created but its request-type label was not applied.

    Gitea accepts an issue (and its attachments) from an account with only
    Read access to the repository, but silently drops the 'labels' it was
    asked to set -- applying labels requires Write access. We surface that as
    an error rather than quietly filing an untagged request that would slip
    past triage. The created (but unlabeled) issue is named so an operator can
    find it; the fix is to grant the Gitea account Write access to the repo.
    """

    def __init__(self, request_type: str, number: int, url: str):
        self.request_type = request_type
        self.number = number
        self.url = url
        super().__init__(
            f"issue #{number} was created but its {request_type!r} label was "
            f"not applied -- the Gitea account likely lacks Write access to "
            f"the repository (a Read-only account can file issues but Gitea "
            f"silently drops their labels): {url}"
        )


class CreatedGiteaIssue(pydantic.BaseModel):
    """A Gitea issue created on behalf of a room request."""

    number: int
    url: str
    title: str


async def _find_label(
    client: httpx.AsyncClient,
    repo_url: str,
    headers: dict[str, str],
    name: str,
) -> int | None:
    """Return the id of the named repo label, or None if it is not defined.

    Walks the repository's paginated label listing rather than trusting the
    first page: a canonical label sitting past the page boundary must not be
    mistaken for a missing one (see 'soliplex_concierge.labels').
    """
    for page in range(1, MAX_LABEL_PAGES + 1):
        response = await client.get(
            f"{repo_url}/labels",
            headers=headers,
            params={"page": page, "limit": LABEL_PAGE_SIZE},
        )
        response.raise_for_status()
        labels = response.json()
        if not labels:
            return None
        for label in labels:
            if label["name"] == name:
                return label["id"]
    return None


async def _ensure_label(
    client: httpx.AsyncClient,
    repo_url: str,
    headers: dict[str, str],
    name: str,
) -> int:
    """Return the id of the named repo label, creating it if it is absent."""
    label_id = await _find_label(client, repo_url, headers, name)
    if label_id is not None:
        return label_id

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
    name: str,
    exclude_claims: list[str],
) -> None:
    """Attach the requesting user's profile to the issue as a YAML file.

    Fields named in 'exclude_claims' are dropped from the profile before
    conversion to the YAML used for the attachment.
    """
    claims = user.model_dump()
    for claim in exclude_claims:
        claims.pop(claim, None)
    content = yaml.safe_dump(claims, sort_keys=False)
    response = await client.post(
        f"{repo_url}/issues/{number}/assets",
        headers=headers,
        params={"name": name},
        files={
            "attachment": (
                name,
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
    collected.

    `request_type` is used to tag the issue a type label to allow
    administrators to triage it.

    The requesting user's profile is attached to the issue as a YAML
    file, so you need not transcribe their identity into the 'body'.

    Report the returned issue number and URL back to the user.

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
        LabelNotApplied: if the created issue did not receive its type label
            (typically the Gitea account lacks Write access to the repo).
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

        applied = {label["name"] for label in data.get("labels", [])}
        if request_type not in applied:
            raise LabelNotApplied(
                request_type, data["number"], data["html_url"]
            )

        await _attach_user_profile(
            client,
            repo_url,
            headers,
            data["number"],
            ctx.deps.user,
            tool_config.profile_attachment_name,
            tool_config.exclude_claims,
        )

    return CreatedGiteaIssue(
        number=data["number"],
        url=data["html_url"],
        title=data["title"],
    )
