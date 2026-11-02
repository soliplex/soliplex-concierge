"""Checks that a room provisioner implementation keeps the API's contract.

A deployment serving :mod:`soliplex_concierge.provisioner.api` checks its
implementation from its own tests: it supplies a :class:`Harness` (its
application, ready to admit requests, plus the means to put it in the
states the checks need), then awaits :func:`check_all` or single
``check_*`` functions::

    @pytest.mark.anyio
    async def test_keeps_the_provisioner_contract(harness):
        await contract.check_all(harness)

Each check drives the application in-process (``httpx2.ASGITransport``,
which does not run its lifespan) and raises :class:`ContractViolation`,
saying what the application answered, when it breaks the contract. The
checks request rooms with fresh, unique names, so they do not depend on
one another; a check that requests a room may leave it created.
"""

import collections.abc
import typing
import uuid

import httpx2
import pydantic

from soliplex_concierge.provisioner import api

BASE_URL = "http://provisioner"
NOT_A_TOKEN = "not-a-service-token"


class ContractViolation(AssertionError):
    """The implementation broke the contract."""


class MalformedAnswer(ContractViolation):
    """An answer did not parse as the contract's model for it."""

    def __init__(self, what: str, model: type, text: str):
        super().__init__(
            f"{what}: the answer does not parse as {model.__name__}: {text}"
        )


class Harness(typing.Protocol):
    """An implementation under test, and the means to set its stage.

    - ``app``: the ASGI application, ready to admit requests (its
      ``GET /ok`` answers ``200``)
    - ``admitted_token``: a valid service token for a requester the
      application admits, who is not an admin
    - ``other_token``: a valid service token for another such requester,
      whose rooms get another owner part
    - ``admin_token``: a valid service token for an admin
    - ``not_admitted_token``: a valid service token for a requester the
      application does not admit
    - ``occupy(name)``: make the room that ``admitted_token``'s requester
      would get for a request named ``name`` exist already
    """

    app: typing.Any
    admitted_token: str
    other_token: str
    admin_token: str
    not_admitted_token: str

    def occupy(self, name: str) -> None: ...


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def _unique_name(check: str) -> str:
    return f"contract {check} {uuid.uuid4().hex[:12]}"


async def _request(
    harness: Harness,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: typing.Any = None,
) -> httpx2.Response:
    """Send one request to the application, checking its version header."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    transport = httpx2.ASGITransport(app=harness.app)
    async with httpx2.AsyncClient(
        transport=transport, base_url=BASE_URL
    ) as client:
        response = await client.request(
            method, path, headers=headers, json=body
        )
    version = response.headers.get(api.API_VERSION_HEADER)
    _expect(
        version == api.API_VERSION,
        f"{method} {path}: expected the header "
        f"'{api.API_VERSION_HEADER}: {api.API_VERSION}', got {version!r}",
    )
    return response


def _expect_status(response: httpx2.Response, status: int, what: str) -> None:
    _expect(
        response.status_code == status,
        f"{what}: expected HTTP {status}, got HTTP {response.status_code}: "
        f"{response.text}",
    )


def _parse[M: pydantic.BaseModel](
    model: type[M], response: httpx2.Response, what: str
) -> M:
    try:
        return model.model_validate_json(response.content)
    except pydantic.ValidationError as error:
        raise MalformedAnswer(what, model, response.text) from error


async def _post_room(harness: Harness, token: str, **body) -> httpx2.Response:
    return await _request(
        harness, "POST", api.ROOMS_PATH, token=token, body=body
    )


async def check_health(harness: Harness) -> None:
    """``GET /ok`` answers ``200`` with ``OK``, with no token."""
    response = await _request(harness, "GET", api.HEALTH_PATH)

    _expect_status(response, 200, "GET /ok")
    _expect(
        response.text.strip() == "OK",
        f"GET /ok: expected 'OK', got {response.text!r}",
    )


async def check_requires_a_token(harness: Harness) -> None:
    """Both ``/rooms`` routes answer ``401`` to a request with no token."""
    name = _unique_name("requires-a-token")
    posted = await _request(
        harness, "POST", api.ROOMS_PATH, body={"name": name}
    )
    got = await _request(harness, "GET", api.room_path("nobody:nothing"))

    for response, what in (
        (posted, "POST /rooms with no token"),
        (got, "GET /rooms/{room_id} with no token"),
    ):
        _expect_status(response, 401, what)
        _expect(
            response.headers.get("WWW-Authenticate", "").startswith("Bearer"),
            f"{what}: expected a 'WWW-Authenticate: Bearer' header",
        )
        _parse(api.Error, response, what)


async def check_refuses_an_invalid_token(harness: Harness) -> None:
    """``POST /rooms`` answers ``401`` to a token that is not valid."""
    name = _unique_name("invalid-token")

    response = await _post_room(harness, NOT_A_TOKEN, name=name)

    what = "POST /rooms with an invalid token"
    _expect_status(response, 401, what)
    _parse(api.Error, response, what)


async def check_refuses_the_unadmitted(harness: Harness) -> None:
    """``POST /rooms`` answers ``403`` to a requester it does not admit."""
    name = _unique_name("unadmitted")

    response = await _post_room(harness, harness.not_admitted_token, name=name)

    what = "POST /rooms by an unadmitted requester"
    _expect_status(response, 403, what)
    _parse(api.Error, response, what)


async def check_rejects_a_malformed_body(harness: Harness) -> None:
    """``POST /rooms`` answers ``422`` to a body that is not a RoomRequest.

    Both an unknown field and a missing ``name`` make it one.
    """
    name = _unique_name("malformed-body")
    extra = await _post_room(
        harness, harness.admitted_token, name=name, colour="blue"
    )
    nameless = await _post_room(harness, harness.admitted_token)

    _expect_status(extra, 422, "POST /rooms with an unknown field")
    _expect_status(nameless, 422, "POST /rooms with no name")


async def check_rejects_a_bad_slug(harness: Harness) -> None:
    """``POST /rooms`` answers ``400`` to a slug that is no path segment."""
    name = _unique_name("bad-slug")

    response = await _post_room(
        harness, harness.admitted_token, name=name, slug="../escape"
    )

    what = "POST /rooms with the slug '../escape'"
    _expect_status(response, 400, what)
    _parse(api.Error, response, what)


async def check_accepts_and_reports(harness: Harness) -> None:
    """An admitted request answers ``202``; its requester sees its state."""
    name = _unique_name("accepts")

    posted = await _post_room(harness, harness.admitted_token, name=name)
    _expect_status(posted, 202, "POST /rooms by an admitted requester")
    accepted = _parse(api.Accepted, posted, "POST /rooms")
    path = api.room_path(accepted.room_id)
    got = await _request(harness, "GET", path, token=harness.admitted_token)

    what = f"GET {path} by its requester"
    _expect_status(got, 200, what)
    state = _parse(api.RequestState, got, what)
    _expect(
        state.room_id == accepted.room_id,
        f"{what}: expected room_id {accepted.room_id!r}, "
        f"got {state.room_id!r}",
    )


async def check_hides_requests_from_others(harness: Harness) -> None:
    """Another requester gets ``404`` for a request; an admin sees it."""
    name = _unique_name("hides")
    posted = await _post_room(harness, harness.admitted_token, name=name)
    _expect_status(posted, 202, "POST /rooms by an admitted requester")
    path = api.room_path(_parse(api.Accepted, posted, "POST /rooms").room_id)

    other = await _request(harness, "GET", path, token=harness.other_token)
    admin = await _request(harness, "GET", path, token=harness.admin_token)

    _expect_status(other, 404, f"GET {path} by another requester")
    _expect_status(admin, 200, f"GET {path} by an admin")
    _parse(api.RequestState, admin, f"GET {path} by an admin")


async def check_reports_unknown_requests(harness: Harness) -> None:
    """``GET /rooms/{room_id}`` answers ``404`` for a room never requested."""
    path = api.room_path(f"nobody:{uuid.uuid4().hex}")

    response = await _request(
        harness, "GET", path, token=harness.admitted_token
    )

    what = f"GET {path}, never requested"
    _expect_status(response, 404, what)
    _parse(api.Error, response, what)


async def check_refuses_an_existing_room(harness: Harness) -> None:
    """``POST /rooms`` answers ``409``, naming the room, when it exists."""
    name = _unique_name("existing")
    harness.occupy(name)

    response = await _post_room(harness, harness.admitted_token, name=name)

    what = "POST /rooms for an existing room"
    _expect_status(response, 409, what)
    _parse(api.Refusal, response, what)


Check = collections.abc.Callable[[Harness], collections.abc.Awaitable[None]]

ALL_CHECKS: tuple[Check, ...] = (
    check_health,
    check_requires_a_token,
    check_refuses_an_invalid_token,
    check_refuses_the_unadmitted,
    check_rejects_a_malformed_body,
    check_rejects_a_bad_slug,
    check_accepts_and_reports,
    check_hides_requests_from_others,
    check_reports_unknown_requests,
    check_refuses_an_existing_room,
)


async def check_all(harness: Harness) -> None:
    """Run every check in :data:`ALL_CHECKS`, stopping at the first failure."""
    for check in ALL_CHECKS:
        await check(harness)
