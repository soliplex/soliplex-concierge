"""Tools asking a stack's room provisioner service for a new room.

A 'soliplex-template' stack's 'soliplex-provisioner' service creates a
private RAG room for a requester, and files its tracking issue. These tools
call it on the requesting user's behalf: each call carries a short-lived
service token holding the user's claims, signed with the installation's
'URL_SAFE_TOKEN_SECRET', which the service validates.

The service admits a requester who is an admin, or who holds WRITE on the
reserved room id '__provision__'. Any other user's request is reported as
not admitted, for the agent to file as a 'new-room' issue instead.
"""

import typing
import urllib.parse

import httpx2
import pydantic
import pydantic_ai
from soliplex import agents
from soliplex import models
from soliplex import token_mint

from soliplex_concierge import config
from soliplex_concierge import tls

SERVICE_TOKEN_SECRET = "URL_SAFE_TOKEN_SECRET"
SERVICE_TOKEN_LIFETIME = 60
REQUEST_TIMEOUT = 30

# What 'UserProfile.from_user_claims' fills a missing profile claim with.
UNKNOWN_CLAIM = "<unknown>"

_FALL_BACK = (
    "File the request as a 'new-room' issue instead, as for a user the "
    "provisioner does not admit."
)


class AnonymousUser(pydantic_ai.ToolFailed):
    def __init__(self):
        super().__init__(
            "cannot request a room for an anonymous user (no user profile "
            "is attached to this run)"
        )


class SigningSecretMissing(pydantic_ai.ToolFailed):
    def __init__(self, secret_name: str):
        self.secret_name = secret_name
        super().__init__(
            f"the installation has no {secret_name!r} secret to sign the "
            f"request with. {_FALL_BACK}"
        )


class ServiceUnavailable(pydantic_ai.ToolFailed):
    """The service could not be reached."""

    def __init__(self, error: Exception):
        self.error = error
        super().__init__(
            f"the room provisioner service is unavailable ({error!r}). "
            f"{_FALL_BACK}"
        )


class ServiceTimedOut(pydantic_ai.ToolFailed):
    """The service did not answer in time."""

    def __init__(self, error: Exception):
        self.error = error
        super().__init__(
            f"the room provisioner service did not answer in time "
            f"({error!r}); try again shortly"
        )


class RequestTimedOut(pydantic_ai.ToolFailed):
    """A room request went unanswered, twice: it may still be granted."""

    def __init__(self):
        super().__init__(
            "the room provisioner did not answer in time, so the room may "
            "still be created. Do not file the request as an issue: tell "
            "the user to look for the room in their room list, and for its "
            "tracking issue, in a few minutes"
        )


class ServiceFailed(pydantic_ai.ToolFailed):
    """The service answered with an error the agent cannot act on."""

    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(
            f"the room provisioner service failed (HTTP {status}: "
            f"{detail}). {_FALL_BACK}"
        )


class BadSlug(pydantic_ai.ModelRetry):
    def __init__(self, slug: str, detail: str):
        self.slug, self.detail = slug, detail
        super().__init__(
            f"the slug {slug!r} is unusable ({detail}): choose another, or "
            "omit it to derive one from the name"
        )


class BadRoomName(pydantic_ai.ToolFailed):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(
            f"the room name is unusable ({detail}): ask the user for another "
            "name"
        )


class RoomTaken(pydantic_ai.ToolFailed):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{detail}: ask the user for a different room name")


class UnknownRequest(pydantic_ai.ToolFailed):
    def __init__(self, room_id: str):
        self.room_id = room_id
        super().__init__(
            f"the room provisioner has no request for room {room_id!r} (it "
            "may have restarted): its tracking issue, if any, records the "
            "request"
        )


class RoomRequestAccepted(pydantic.BaseModel):
    """The provisioner service's answer to a room request.

    'status' is 'accepted' when the service is creating the room,
    'already_requested' when an earlier attempt of the same request reached
    it, and 'not_admitted' when it does not admit the user.
    """

    status: typing.Literal["accepted", "already_requested", "not_admitted"]
    room_id: str | None = None
    issue_url: str | None = None


class RoomRequestState(pydantic.BaseModel):
    """The state of a room request the provisioner service accepted."""

    room_id: str
    state: typing.Literal["pending", "live", "incomplete", "failed"]
    issue_url: str | None = None
    incomplete: list[str] = []
    error: str | None = None


def service_claims(user: models.UserProfile | None) -> dict:
    """The claims to mint the user's service token with.

    Drops the claims a service token sets itself ('iat', 'exp'), and the
    placeholders 'UserProfile' fills missing profile claims with. Raises
    'AnonymousUser' for an anonymous run.
    """
    if user is None:
        raise AnonymousUser()
    return {
        claim: value
        for claim, value in user.model_dump().items()
        if claim not in token_mint.SERVICE_TOKEN_RESERVED_CLAIMS
        and value != UNKNOWN_CLAIM
    }


def _service_token(deps: agents.AgentDependencies) -> str:
    claims = service_claims(deps.user)
    try:
        secret = deps.the_installation.get_secret(SERVICE_TOKEN_SECRET)
    except KeyError as error:
        raise SigningSecretMissing(SERVICE_TOKEN_SECRET) from error
    return token_mint.mint_service_token(
        secret, claims, expires_in=SERVICE_TOKEN_LIFETIME
    )


def _room_id(response: httpx2.Response) -> str | None:
    """The 'room_id' a refusal names, if any."""
    try:
        room_id = response.json()["room_id"]
    except (ValueError, TypeError, KeyError):
        return None
    return room_id if isinstance(room_id, str) else None


def _detail(response: httpx2.Response) -> str:
    try:
        return str(response.json()["detail"])
    except (ValueError, TypeError, KeyError):
        return response.text


async def _call(
    deps: agents.AgentDependencies,
    kind: str,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> httpx2.Response:
    """Call the provisioner service as the requesting user.

    Raises 'ServiceTimedOut' when it does not answer within ``timeout``
    seconds, and 'ServiceUnavailable' when it cannot be reached.
    """
    token = _service_token(deps)
    url = deps.tool_configs[kind].service_url.rstrip("/") + path
    try:
        async with httpx2.AsyncClient(verify=tls.httpx_verify()) as client:
            return await client.request(
                method,
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
    except httpx2.TimeoutException as error:
        raise ServiceTimedOut(error) from error
    except httpx2.HTTPError as error:
        raise ServiceUnavailable(error) from error


async def request_room(
    ctx: pydantic_ai.RunContext[agents.AgentDependencies],
    name: str,
    description: str | None = None,
    slug: str | None = None,
) -> RoomRequestAccepted:
    """Ask the room provisioner to create a new private room for the user.

    The room's id is '<owner>:<slug>': the owner comes from the user's
    identity, the slug from 'name' unless you pass one. Only the user gets
    access to the room.

    When the result's 'status' is 'accepted', the room is being created:
    tell the user its 'room_id' and tracking issue ('issue_url', when
    present), and that you can check whether it is ready with
    'check_room_request'. When it is 'already_requested', an earlier
    attempt of this request reached the provisioner: tell the user its
    'room_id', and check on it with 'check_room_request'. When it is
    'not_admitted', the user may not have rooms created on request: file
    the request as a 'new-room' issue instead.

    Args:
        name: the room's human-readable name, as the user gave it.
        description: a one-line description of the room's purpose; defaults
            to the name.
        slug: the room id's second part (letters, digits, '.', '_', '-');
            omit it to derive one from the name.

    Returns:
        RoomRequestAccepted: the 'status', and for an accepted request its
            'room_id' and 'issue_url'.
    """
    body = {"name": name, "description": description, "slug": slug}
    timeout = ctx.deps.tool_configs[config.RR_TOOL_KIND].request_timeout

    async def post():
        return await _call(
            ctx.deps, config.RR_TOOL_KIND, "POST", "/rooms", body, timeout
        )

    retried = False
    try:
        response = await post()
    except ServiceTimedOut:
        # The request may have reached the service: ask again, once.
        try:
            response = await post()
        except ServiceTimedOut as error:
            raise RequestTimedOut() from error
        retried = True

    if retried and response.status_code == 409:
        room_id = _room_id(response)
        if room_id is None:
            raise RequestTimedOut()
        return RoomRequestAccepted(status="already_requested", room_id=room_id)
    if response.status_code == 202:
        return RoomRequestAccepted(status="accepted", **response.json())
    if response.status_code == 403:
        return RoomRequestAccepted(status="not_admitted")
    if response.status_code == 400:
        if slug is not None:
            raise BadSlug(slug, _detail(response))
        raise BadRoomName(_detail(response))
    if response.status_code == 409:
        raise RoomTaken(_detail(response))
    raise ServiceFailed(response.status_code, _detail(response))


async def check_room_request(
    ctx: pydantic_ai.RunContext[agents.AgentDependencies],
    room_id: str,
) -> RoomRequestState:
    """Check on a room the provisioner accepted with 'request_room'.

    Report the result's 'state' to the user:

    - 'pending': the room is still being created
    - 'live': the room is ready; the user should refresh their room list
    - 'incomplete': the room exists, but an administrator must finish the
      steps listed in 'incomplete'
    - 'failed': the room was not created; 'error' says why

    Args:
        room_id: the 'room_id' that 'request_room' returned.

    Returns:
        RoomRequestState: the request's 'state', with its 'issue_url', any
            'incomplete' steps, and any 'error'.
    """
    path = f"/rooms/{urllib.parse.quote(room_id, safe=':')}"
    response = await _call(ctx.deps, config.CRR_TOOL_KIND, "GET", path)

    if response.status_code == 200:
        return RoomRequestState(**response.json())
    if response.status_code == 404:
        raise UnknownRequest(room_id)
    raise ServiceFailed(response.status_code, _detail(response))
