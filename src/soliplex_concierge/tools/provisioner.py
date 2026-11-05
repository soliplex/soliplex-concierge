"""Tools asking a stack's room provisioner service for a new room.

A 'soliplex-template' stack's 'soliplex-provisioner' service creates a
private RAG room for a requester, and files its tracking issue. These tools
call it on the requesting user's behalf: each call carries a short-lived
service token holding the user's claims, signed with the installation's
'URL_SAFE_TOKEN_SECRET', which the service validates.

The service admits a requester who is an admin, or who holds WRITE on the
reserved room id '__provision__'. Any other user's request is reported as
not admitted, for the agent to file as a 'new-room' issue instead.

The service's API is the contract in 'soliplex_concierge.provisioner.api'.
"""

import typing

import httpx2
import pydantic
import pydantic_ai
from soliplex import agents
from soliplex import models
from soliplex import token_mint

from soliplex_concierge import config
from soliplex_concierge import tls
from soliplex_concierge.provisioner import api
from soliplex_concierge.provisioner import soliplex_api

SERVICE_TOKEN_SECRET = api.SERVICE_TOKEN_SECRET
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


class IncompatibleService(pydantic_ai.ToolFailed):
    """The service implements another version of the API."""

    def __init__(self, version: str):
        self.version = version
        super().__init__(
            f"the room provisioner service implements version {version!r} "
            f"of its API, not {api.API_VERSION!r}. {_FALL_BACK}"
        )


class BadSlug(pydantic_ai.ModelRetry):
    def __init__(self, slug: str, detail: str):
        self.slug, self.detail = slug, detail
        super().__init__(
            f"the slug {slug!r} is unusable ({detail}): choose another, or "
            "omit it to derive one from the name"
        )


class OwnerIncomplete(pydantic_ai.ModelRetry):
    def __init__(self):
        super().__init__(
            "to name a room's owner, pass 'owner' with either both "
            "'owner_claim' and 'owner_value', or 'owner_predicate'; to "
            "request the room as the user's own, pass none of them"
        )


class UnknownPredicateName(pydantic_ai.ModelRetry):
    def __init__(self, predicate: str, known: frozenset[str]):
        self.predicate, self.known = predicate, known
        listed = ", ".join(sorted(known)) or "none"
        super().__init__(
            f"{predicate!r} is not a predicate this installation registers "
            f"(it registers: {listed}): offer the user one of those, or "
            "name the owner by a claim and value"
        )


class BadOwner(pydantic_ai.ModelRetry):
    def __init__(self, owner: str, detail: str):
        self.owner, self.detail = owner, detail
        super().__init__(
            f"the request naming the owner {owner!r} is unusable ({detail}): "
            "correct the owner, its claim, value or predicate, or the slug, "
            "with the user, "
            "or omit the owner to request the room as the user's own"
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
    it, and 'not_admitted' when it does not admit the user. A request
    naming an owner gets 'owner_not_permitted' instead of 'not_admitted':
    only an admin may name a room's owner.
    """

    status: typing.Literal[
        "accepted",
        "already_requested",
        "not_admitted",
        "owner_not_permitted",
    ]
    room_id: str | None = None
    issue_url: str | None = None


# The state of a room request the provisioner service accepted.
RoomRequestState = api.RequestState


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
        refusal = api.Refusal.model_validate_json(response.content)
    except pydantic.ValidationError:
        return None
    return refusal.room_id


def _detail(response: httpx2.Response) -> str:
    try:
        error = api.Error.model_validate_json(response.content)
    except pydantic.ValidationError:
        return response.text
    return str(error.detail)


def _answer[M: pydantic.BaseModel](
    model: type[M], response: httpx2.Response
) -> M:
    """The service's answer, as ``model``.

    Raises 'ServiceFailed' when the answer is not one.
    """
    try:
        return model.model_validate_json(response.content)
    except pydantic.ValidationError as error:
        raise ServiceFailed(
            response.status_code, f"malformed answer: {response.text}"
        ) from error


def _check_version(response: httpx2.Response) -> None:
    """Raise 'IncompatibleService' for another version of the API.

    An answer without the version header is taken to be version 1's: the
    services that predate the header implement it.
    """
    version = response.headers.get(api.API_VERSION_HEADER)
    if version is not None and version != api.API_VERSION:
        raise IncompatibleService(version)


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
    seconds, 'ServiceUnavailable' when it cannot be reached, and
    'IncompatibleService' when it implements another version of the API.
    """
    token = _service_token(deps)
    url = deps.tool_configs[kind].service_url.rstrip("/") + path
    try:
        async with httpx2.AsyncClient(verify=tls.httpx_verify()) as client:
            response = await client.request(
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
    _check_version(response)
    return response


def _named_owner(owner, claim, value, predicate) -> api.RoomOwner | None:
    """The owner a request names, if any, in one of its two forms.

    Raises 'OwnerIncomplete' for any other combination, and
    'UnknownPredicateName' for a predicate the installation does not register.
    """
    by_claim = claim is not None and value is not None
    by_predicate = predicate is not None
    partial = (claim is None) != (value is None)
    if owner is None:
        if by_claim or by_predicate or partial:
            raise OwnerIncomplete()
        return None
    if partial or by_claim == by_predicate:
        raise OwnerIncomplete()
    if by_predicate:
        known = soliplex_api.registered_predicates()
        if predicate not in known:
            raise UnknownPredicateName(predicate, known)
        return api.RoomOwner(owner=owner, predicate=predicate)
    return api.RoomOwner(owner=owner, claim=claim, value=value)


async def request_room(
    ctx: pydantic_ai.RunContext[agents.AgentDependencies],
    name: str,
    description: str | None = None,
    slug: str | None = None,
    owner: str | None = None,
    owner_claim: str | None = None,
    owner_value: str | None = None,
    owner_predicate: str | None = None,
) -> RoomRequestAccepted:
    """Ask the room provisioner to create a new private room.

    The room's id joins an owner part and a slug (e.g.
    'alice--q3-plans'): the owner comes from the user's identity, the slug
    from 'name' unless you pass one. Only the owner gets access to the
    room.

    An administrator may ask for a room owned by someone else: pass
    'owner', with whom it grants the room in one of two forms, and only
    then. The service refuses an owner from anyone but an administrator.

    - 'owner_claim' and 'owner_value': users whose token claim is that
      value (e.g. 'email' is 'bharney@example.com')
    - 'owner_predicate': users whose claims pass a predicate the
      installation registers (e.g. a role or group membership check)

    When the result's 'status' is 'accepted', the room is being created:
    tell the user its 'room_id' and tracking issue ('issue_url', when
    present), and that you can check whether it is ready with
    'check_room_request'. When it is 'already_requested', an earlier
    attempt of this request reached the provisioner: tell the user its
    'room_id', and check on it with 'check_room_request'. When it is
    'not_admitted', the user may not have rooms created on request: file
    the request as a 'new-room' issue instead. When it is
    'owner_not_permitted', the user may not name a room's owner: offer to
    request the room as their own, or file a 'new-room' issue naming the
    intended owner.

    Args:
        name: the room's human-readable name, as the user gave it.
        description: a one-line description of the room's purpose; defaults
            to the name.
        slug: the room id's second part: lowercase letters and digits,
            joined by single '-'; omit it to derive one from the name.
        owner: the room id's first part, for a room owned by someone other
            than the user (letters, digits, '.', '_', '-').
        owner_claim: with 'owner_value', the claim a user must hold.
        owner_value: with 'owner_claim', the value it must have.
        owner_predicate: instead of a claim and value, the name of a
            predicate the installation registers.

    Returns:
        RoomRequestAccepted: the 'status', and for an accepted request its
            'room_id' and 'issue_url'.
    """
    fields = {"name": name, "description": description, "slug": slug}
    named = _named_owner(owner, owner_claim, owner_value, owner_predicate)
    if named is not None:
        fields["owner"] = named
    # Unset fields stay out: services that predate them refuse them.
    body = api.RoomRequest(**fields).model_dump(exclude_unset=True)
    timeout = ctx.deps.tool_configs[config.RR_TOOL_KIND].request_timeout

    async def post():
        return await _call(
            ctx.deps,
            config.RR_TOOL_KIND,
            "POST",
            api.ROOMS_PATH,
            body,
            timeout,
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
        accepted = _answer(api.Accepted, response)
        return RoomRequestAccepted(status="accepted", **accepted.model_dump())
    if response.status_code == 403:
        if owner is not None:
            return RoomRequestAccepted(status="owner_not_permitted")
        return RoomRequestAccepted(status="not_admitted")
    if response.status_code == 400:
        if owner is not None:
            raise BadOwner(owner, _detail(response))
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
    path = api.room_path(room_id)
    response = await _call(ctx.deps, config.CRR_TOOL_KIND, "GET", path)

    if response.status_code == 200:
        return _answer(RoomRequestState, response)
    if response.status_code == 404:
        raise UnknownRequest(room_id)
    raise ServiceFailed(response.status_code, _detail(response))
