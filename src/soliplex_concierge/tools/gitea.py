"""Tools supporting the Gitea integration used by the 'about_soliplex' room.

Issue creation talks to the Gitea REST API directly (no MCP server): the
repository is fixed by the room's tool configuration, so the agent cannot
file against an arbitrary repository.
"""

import contextlib
import typing
from collections.abc import Iterable
from collections.abc import Mapping

import httpx2
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


# How the requesting user's profile was recorded on their issue:
# - 'attached': as the YAML attachment
# - 'commented': as a comment holding the YAML, Gitea having refused the
#   attachment
# - 'missing': not at all, Gitea having refused the comment too
ProfileRecord = typing.Literal["attached", "commented", "missing"]


class CreatedGiteaIssue(pydantic.BaseModel):
    """A Gitea issue created on behalf of a room request."""

    number: int
    url: str
    title: str
    profile: ProfileRecord = "attached"


# The request sequence that files an issue is written once, sans-IO: each
# step is a generator that yields '(method, url, kwargs)' for every HTTP call
# it needs and is sent back the response. '_drive_sync' / '_drive_async' run
# it against an 'httpx2.Client' / 'httpx2.AsyncClient', so the room tool and
# its synchronous counterpart cannot drift apart.
_Request = tuple[str, str, dict[str, typing.Any]]
_Steps = typing.Generator[_Request, httpx2.Response, typing.Any]


def _find_label(
    repo_url: str,
    headers: dict[str, str],
    name: str,
) -> _Steps:
    """Return the id of the named repo label, or None if it is not defined.

    Walks the repository's paginated label listing rather than trusting the
    first page: a canonical label sitting past the page boundary must not be
    mistaken for a missing one (see 'soliplex_concierge.labels').
    """
    for page in range(1, MAX_LABEL_PAGES + 1):
        response = yield (
            "get",
            f"{repo_url}/labels",
            {
                "headers": headers,
                "params": {"page": page, "limit": LABEL_PAGE_SIZE},
            },
        )
        response.raise_for_status()
        labels = response.json()
        if not labels:
            return None
        for label in labels:
            if label["name"] == name:
                return label["id"]
    return None


def _ensure_label(
    repo_url: str,
    headers: dict[str, str],
    name: str,
) -> _Steps:
    """Return the id of the named repo label, creating it if it is absent."""
    label_id = yield from _find_label(repo_url, headers, name)
    if label_id is not None:
        return label_id

    spec = ISSUE_TYPE_LABELS[name]
    response = yield (
        "post",
        f"{repo_url}/labels",
        {
            "headers": headers,
            "json": {
                "name": name,
                "color": spec["color"],
                "description": spec["description"],
            },
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def _failure(error: Exception) -> str:
    """A short description of a failed request, for an issue comment."""
    if isinstance(error, httpx2.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def _record_user_profile(
    repo_url: str,
    headers: dict[str, str],
    number: int,
    claims: dict[str, typing.Any],
    name: str,
    exclude_claims: list[str],
) -> _Steps:
    """Record the requesting user's profile on the issue; return how.

    Attaches it as a YAML file. When Gitea refuses the attachment (e.g. a
    server whose '[attachment] ALLOWED_TYPES' lacks the name's extension),
    comments it on the issue instead, as a fenced YAML block; when Gitea
    refuses that too, the issue keeps no profile. Fields named in
    'exclude_claims' are dropped from the profile first. Returns a
    'ProfileRecord'.
    """
    claims = dict(claims)
    for claim in exclude_claims:
        claims.pop(claim, None)
    content = yaml.safe_dump(claims, sort_keys=False)
    try:
        response = yield (
            "post",
            f"{repo_url}/issues/{number}/assets",
            {
                "headers": headers,
                "params": {"name": name},
                "files": {
                    "attachment": (
                        name,
                        content.encode("utf-8"),
                        "application/x-yaml",
                    )
                },
            },
        )
        response.raise_for_status()
    except httpx2.HTTPError as error:
        attach_error = error
    else:
        return "attached"

    body = (
        f"The requester's profile (attaching it as `{name}` failed: "
        f"{_failure(attach_error)}):\n\n```yaml\n{content}```\n"
    )
    try:
        response = yield (
            "post",
            f"{repo_url}/issues/{number}/comments",
            {"headers": headers, "json": {"body": body}},
        )
        response.raise_for_status()
    except httpx2.HTTPError:
        return "missing"
    return "commented"


def _file_issue(
    *,
    host: str,
    token: str,
    owner: str,
    repo: str,
    profile_attachment_name: str,
    exclude_claims: list[str],
    claims: dict[str, typing.Any],
    title: str,
    body: str,
    request_type: str,
) -> _Steps:
    """Ensure the type label, file the issue, and record the user profile.

    Once the issue exists, recording the profile is best-effort (see
    '_record_user_profile'): the issue is returned however that goes.
    """
    repo_url = f"{host.rstrip('/')}/api/v1/repos/{owner}/{repo}"
    headers = {"Authorization": f"token {token}"}

    label_id = yield from _ensure_label(repo_url, headers, request_type)
    response = yield (
        "post",
        f"{repo_url}/issues",
        {
            "headers": headers,
            "json": {"title": title, "body": body, "labels": [label_id]},
        },
    )
    response.raise_for_status()
    data = response.json()

    applied = {label["name"] for label in data.get("labels", [])}
    if request_type not in applied:
        raise LabelNotApplied(request_type, data["number"], data["html_url"])

    profile = yield from _record_user_profile(
        repo_url,
        headers,
        data["number"],
        claims,
        profile_attachment_name,
        exclude_claims,
    )

    return CreatedGiteaIssue(
        number=data["number"],
        url=data["html_url"],
        title=data["title"],
        profile=profile,
    )


def _drive_sync(client: httpx2.Client, steps: _Steps) -> typing.Any:
    """Run 'steps' to completion, issuing its requests via 'client'.

    An error raised by a request is thrown into 'steps' at the 'yield' that
    asked for it, so the step sees (and its frames appear in the traceback
    of) its own failure; 'steps' is closed however the drive ends.
    """
    with contextlib.closing(steps):
        try:
            method, url, kwargs = next(steps)
            while True:
                try:
                    response = getattr(client, method)(url, **kwargs)
                except Exception as exc:
                    method, url, kwargs = steps.throw(exc)
                else:
                    method, url, kwargs = steps.send(response)
        except StopIteration as stop:
            return stop.value


async def _drive_async(
    client: httpx2.AsyncClient, steps: _Steps
) -> typing.Any:
    """Run 'steps' to completion, awaiting its requests via 'client'.

    See '_drive_sync': request errors are thrown into 'steps', which is
    closed however the drive ends (including on cancellation).
    """
    with contextlib.closing(steps):
        try:
            method, url, kwargs = next(steps)
            while True:
                try:
                    response = await getattr(client, method)(url, **kwargs)
                except Exception as exc:
                    method, url, kwargs = steps.throw(exc)
                else:
                    method, url, kwargs = steps.send(response)
        except StopIteration as stop:
            return stop.value


def create_gitea_issue_sync(
    *,
    host: str,
    token: str,
    owner: str,
    repo: str,
    user: models.UserProfile | Mapping[str, typing.Any],
    title: str,
    body: str,
    request_type: str,
    profile_attachment_name: str = config.DEFAULT_PROFILE_ATTACHMENT_NAME,
    exclude_claims: Iterable[str] = (),
) -> CreatedGiteaIssue:
    """File a room request as a Gitea issue, outside any agent run.

    The synchronous counterpart of the 'create_gitea_issue' room tool, for
    callers (e.g. a room provisioner) with no pydantic-ai run context: the
    connection and profile settings the tool reads from its config, and the
    requesting user it reads from the run's dependencies, are passed in
    explicitly. The issue filed is the same one the tool would file.

    Args:
        host: the Gitea base URL, e.g. 'https://gitea.example.com'.
        token: a Gitea access token with Write access to the repository.
        owner: the tracking repository's owner.
        repo: the tracking repository's name.
        user: the requesting user's profile, or a mapping of their claims;
            attached to the issue as a YAML file (or, when Gitea refuses
            the attachment, commented on it).
        title: the issue title.
        body: the issue body (Markdown).
        request_type: 'new-room' or 'room-access'.
        profile_attachment_name: filename the profile is attached under.
        exclude_claims: claims dropped from the profile before attaching it.

    Returns:
        CreatedGiteaIssue: the 'number', 'url', and 'title' of the new issue,
            and how its 'profile' was recorded ('attached', 'commented' or
            'missing').

    Raises:
        UnknownRequestType: if 'request_type' is not a known issue-type label.
        LabelNotApplied: if the created issue did not receive its type label
            (typically the Gitea account lacks Write access to the repo).
    """
    if request_type not in ISSUE_TYPE_LABELS:
        raise UnknownRequestType(request_type, set(ISSUE_TYPE_LABELS))

    if isinstance(user, models.UserProfile):
        claims = user.model_dump()
    else:
        claims = dict(user)

    steps = _file_issue(
        host=host,
        token=token,
        owner=owner,
        repo=repo,
        profile_attachment_name=profile_attachment_name,
        exclude_claims=list(exclude_claims),
        claims=claims,
        title=title,
        body=body,
        request_type=request_type,
    )
    with httpx2.Client(verify=tls.httpx_verify()) as client:
        return _drive_sync(client, steps)


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
    file (or commented on it), so you need not transcribe their identity
    into the 'body'.

    Report the returned issue number and URL back to the user. If the
    returned 'profile' is 'missing', also tell them the issue does not
    record who they are, so an administrator may ask.

    Args:
        title: the issue title.
        body: the issue body (Markdown).
        request_type: which kind of request this is -- 'new-room' for a new
            room, or 'room-access' for access to an existing private room
            (the 'TYPE:' line returned by the room-request concierge skill).

    Returns:
        CreatedGiteaIssue: the 'number', 'url', and 'title' of the new issue,
            and how its 'profile' was recorded ('attached', 'commented' or
            'missing').

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

    steps = _file_issue(
        host=tool_config.host,
        token=tool_config.token,
        owner=tool_config.owner,
        repo=tool_config.repo,
        profile_attachment_name=tool_config.profile_attachment_name,
        exclude_claims=tool_config.exclude_claims,
        claims=ctx.deps.user.model_dump(),
        title=title,
        body=body,
        request_type=request_type,
    )
    async with httpx2.AsyncClient(verify=tls.httpx_verify()) as client:
        return await _drive_async(client, steps)
