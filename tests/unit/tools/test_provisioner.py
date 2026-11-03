import json
from unittest import mock

import httpx2
import pydantic_ai
import pytest
from soliplex import agents
from soliplex import installation
from soliplex import models
from soliplex import token_mint

from soliplex_concierge import config
from soliplex_concierge.provisioner import api
from soliplex_concierge.tools import provisioner

SECRET = "test" * 8  # a signing key for the tests
SERVICE_URL = "http://soliplex-provisioner:8090"
ROOM = "phreddy:q3-plans"
ISSUE_URL = "https://gitea.example.com/ops/requests/issues/7"

USER = models.UserProfile.from_user_claims(
    {
        "sub": "phreddy-sub",
        "preferred_username": "phreddy",
        "email": "phreddy@example.com",
        "groups": ["staff"],
        "iat": 1700000000,
        "exp": 1700003600,
    }
)


class _FakeService:
    """Answers requests with 'response'; records each one it is sent.

    ``first`` (an exception) answers the first request instead; ``headers``
    go with each response.
    """

    def __init__(
        self, status=200, payload=None, error=None, first=None, headers=None
    ):
        self.status, self.payload, self.error = status, payload, error
        self.first, self.headers = first, headers
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.first is not None and len(self.requests) == 1:
            raise self.first
        if self.error is not None:
            raise self.error
        if isinstance(self.payload, str):
            return httpx2.Response(
                self.status, text=self.payload, headers=self.headers
            )
        return httpx2.Response(
            self.status, json=self.payload, headers=self.headers
        )


@pytest.fixture
def the_installation():
    the_installation = mock.create_autospec(installation.Installation)
    the_installation.get_secret.return_value = SECRET
    return the_installation


@pytest.fixture
def ctx(the_installation):
    tool_config = mock.Mock(
        service_url=SERVICE_URL + "/", request_timeout=90.0
    )
    deps = agents.AgentDependencies(
        the_installation=the_installation,
        user=USER,
        tool_configs={
            config.RR_TOOL_KIND: tool_config,
            config.CRR_TOOL_KIND: tool_config,
        },
    )
    return mock.Mock(spec_set=(["deps"]), deps=deps)


def _patch_service(service):
    """Patch 'httpx2.AsyncClient' to send every request to 'service'."""
    real_client = httpx2.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx2.MockTransport(service), **kwargs)

    return mock.patch.object(provisioner.httpx2, "AsyncClient", client)


def _claims(request):
    scheme, token = request.headers["Authorization"].split()
    assert scheme == "Bearer"
    claims, reason = token_mint.load_service_token(SECRET, token)
    assert reason is None
    return claims


# ---- service_claims ----


def test_service_claims_drops_reserved_claims_and_placeholders():
    result = provisioner.service_claims(USER)

    assert result == {
        "sub": "phreddy-sub",
        "preferred_username": "phreddy",
        "email": "phreddy@example.com",
        "groups": ["staff"],
    }


def test_service_claims_anonymous():
    with pytest.raises(pydantic_ai.ToolFailed) as caught:
        provisioner.service_claims(None)

    assert type(caught.value) is provisioner.AnonymousUser
    assert "anonymous user" in caught.value.message


# ---- request_room ----


@pytest.mark.anyio
async def test_request_room_accepted(ctx, the_installation):
    service = _FakeService(202, {"room_id": ROOM, "issue_url": ISSUE_URL})

    with _patch_service(service):
        result = await provisioner.request_room(
            ctx, name="Q3 Plans", description="Plans for Q3"
        )

    assert result == provisioner.RoomRequestAccepted(
        status="accepted", room_id=ROOM, issue_url=ISSUE_URL
    )
    [request] = service.requests
    assert request.method == "POST"
    assert str(request.url) == f"{SERVICE_URL}/rooms"
    assert json.loads(request.content) == {
        "name": "Q3 Plans",
        "description": "Plans for Q3",
        "slug": None,
    }
    claims = _claims(request)
    assert claims["sub"] == "phreddy-sub"
    assert claims["groups"] == ["staff"]
    assert "given_name" not in claims
    assert claims["exp"] - claims["iat"] == provisioner.SERVICE_TOKEN_LIFETIME
    the_installation.get_secret.assert_called_once_with(
        provisioner.SERVICE_TOKEN_SECRET
    )


@pytest.mark.anyio
async def test_request_room_not_admitted(ctx):
    service = _FakeService(403, {"detail": "may not request rooms"})

    with _patch_service(service):
        result = await provisioner.request_room(ctx, name="Q3 Plans")

    assert result == provisioner.RoomRequestAccepted(status="not_admitted")


@pytest.mark.anyio
async def test_request_room_bad_slug(ctx):
    service = _FakeService(400, {"detail": "bad slug 'no/slash'"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ModelRetry) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3", slug="no/slash")

    assert type(caught.value) is provisioner.BadSlug
    assert "bad slug 'no/slash'" in caught.value.message


@pytest.mark.anyio
async def test_request_room_bad_name(ctx):
    service = _FakeService(400, {"detail": "room name '  ' must not be blank"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="  ")

    assert type(caught.value) is provisioner.BadRoomName
    assert "must not be blank" in caught.value.message
    assert "ask the user for another name" in caught.value.message


@pytest.mark.anyio
async def test_request_room_taken(ctx):
    service = _FakeService(409, {"detail": f"room {ROOM!r} already exists"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans")

    assert type(caught.value) is provisioner.RoomTaken
    assert "already exists" in caught.value.message
    assert "different room name" in caught.value.message


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status, payload, detail",
    [
        (500, {"detail": "audit failed"}, "HTTP 500: audit failed"),
        (401, {"detail": "not valid"}, "HTTP 401: not valid"),
        (502, "Bad Gateway", "HTTP 502: Bad Gateway"),
        (500, ["unexpected"], 'HTTP 500: ["unexpected"]'),
    ],
)
async def test_request_room_service_failed(ctx, status, payload, detail):
    service = _FakeService(status, payload)

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans")

    assert type(caught.value) is provisioner.ServiceFailed
    assert detail in caught.value.message
    assert "'new-room' issue instead" in caught.value.message


@pytest.mark.anyio
async def test_request_room_service_unreachable(ctx):
    service = _FakeService(error=httpx2.ConnectError("Name not known"))

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans")

    assert type(caught.value) is provisioner.ServiceUnavailable
    assert "unavailable" in caught.value.message
    assert "Name not known" in caught.value.message
    assert "'new-room' issue instead" in caught.value.message


@pytest.mark.anyio
async def test_request_room_without_the_signing_secret(ctx, the_installation):
    the_installation.get_secret.side_effect = KeyError("URL_SAFE_TOKEN_SECRET")
    service = _FakeService(202)

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans")

    assert type(caught.value) is provisioner.SigningSecretMissing
    assert "'URL_SAFE_TOKEN_SECRET'" in caught.value.message
    assert "'new-room' issue instead" in caught.value.message
    assert service.requests == []


_TIMEOUT = httpx2.ReadTimeout("")


@pytest.mark.anyio
async def test_request_room_waits_its_own_timeout(ctx):
    service = _FakeService(202, {"room_id": ROOM, "issue_url": None})

    with _patch_service(service):
        await provisioner.request_room(ctx, name="Q3 Plans")

    [request] = service.requests
    assert request.extensions["timeout"]["read"] == 90.0


@pytest.mark.anyio
async def test_request_room_retries_after_a_timeout(ctx):
    service = _FakeService(
        202, {"room_id": ROOM, "issue_url": ISSUE_URL}, first=_TIMEOUT
    )

    with _patch_service(service):
        result = await provisioner.request_room(ctx, name="Q3 Plans")

    assert result == provisioner.RoomRequestAccepted(
        status="accepted", room_id=ROOM, issue_url=ISSUE_URL
    )
    assert len(service.requests) == 2


@pytest.mark.anyio
async def test_request_room_already_requested_after_a_timeout(ctx):
    service = _FakeService(
        409,
        {
            "detail": f"room {ROOM!r} is already being provisioned",
            "room_id": ROOM,
        },
        first=_TIMEOUT,
    )

    with _patch_service(service):
        result = await provisioner.request_room(ctx, name="Q3 Plans")

    assert result == provisioner.RoomRequestAccepted(
        status="already_requested", room_id=ROOM
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "service",
    [
        _FakeService(error=_TIMEOUT),
        _FakeService(409, {"detail": "room exists"}, first=_TIMEOUT),
        _FakeService(409, {"detail": "x", "room_id": 7}, first=_TIMEOUT),
        _FakeService(409, "conflict", first=_TIMEOUT),
    ],
    ids=["twice", "no-room-id", "not-a-string", "not-json"],
)
async def test_request_room_timed_out(ctx, service):
    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans")

    assert type(caught.value) is provisioner.RequestTimedOut
    assert "Do not file the request as an issue" in caught.value.message
    assert len(service.requests) == 2


STAFF_RULE = "$[?'staff' in @.groups]"


@pytest.mark.anyio
async def test_request_room_for_a_named_owner(ctx):
    service = _FakeService(
        202, {"room_id": "staff:q3-plans", "issue_url": ISSUE_URL}
    )

    with _patch_service(service):
        result = await provisioner.request_room(
            ctx, name="Q3 Plans", owner="staff", owner_json_path=STAFF_RULE
        )

    assert result == provisioner.RoomRequestAccepted(
        status="accepted", room_id="staff:q3-plans", issue_url=ISSUE_URL
    )
    [request] = service.requests
    assert json.loads(request.content) == {
        "name": "Q3 Plans",
        "description": None,
        "slug": None,
        "owner": {"owner": "staff", "json_path": STAFF_RULE},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kwargs",
    [{"owner": "staff"}, {"owner_json_path": STAFF_RULE}],
    ids=["owner-only", "rule-only"],
)
async def test_request_room_with_half_an_owner(ctx, kwargs):
    service = _FakeService(202)

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ModelRetry) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans", **kwargs)

    assert type(caught.value) is provisioner.OwnerIncomplete
    assert service.requests == []


@pytest.mark.anyio
async def test_request_room_owner_not_permitted(ctx):
    service = _FakeService(403, {"detail": "only an admin may name owners"})

    with _patch_service(service):
        result = await provisioner.request_room(
            ctx, name="Q3 Plans", owner="staff", owner_json_path=STAFF_RULE
        )

    assert result == provisioner.RoomRequestAccepted(
        status="owner_not_permitted"
    )


@pytest.mark.anyio
async def test_request_room_bad_owner(ctx):
    service = _FakeService(400, {"detail": "room owner '../x' must match"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ModelRetry) as caught,
    ):
        await provisioner.request_room(
            ctx, name="Q3 Plans", owner="../x", owner_json_path=STAFF_RULE
        )

    assert type(caught.value) is provisioner.BadOwner
    assert caught.value.owner == "../x"
    assert "room owner '../x' must match" in caught.value.message
    assert "as the user's own" in caught.value.message


@pytest.mark.anyio
async def test_request_room_accepted_by_a_versioned_service(ctx):
    service = _FakeService(
        202,
        {"room_id": ROOM, "issue_url": None},
        headers={api.API_VERSION_HEADER: api.API_VERSION},
    )

    with _patch_service(service):
        result = await provisioner.request_room(ctx, name="Q3 Plans")

    assert result == provisioner.RoomRequestAccepted(
        status="accepted", room_id=ROOM
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [{"room": ROOM}, "Accepted"],
    ids=["not-accepted", "not-json"],
)
async def test_request_room_malformed_answer(ctx, payload):
    service = _FakeService(202, payload)

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.request_room(ctx, name="Q3 Plans")

    assert type(caught.value) is provisioner.ServiceFailed
    assert "HTTP 202: malformed answer" in caught.value.message
    assert "'new-room' issue instead" in caught.value.message


async def _request_room(ctx):
    return await provisioner.request_room(ctx, name="Q3 Plans")


async def _check_room_request(ctx):
    return await provisioner.check_room_request(ctx, room_id=ROOM)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "call, status, payload",
    [
        (_request_room, 202, {"room_id": ROOM, "issue_url": None}),
        (_check_room_request, 200, {"room_id": ROOM, "state": "live"}),
        (_check_room_request, 404, {"detail": "no request"}),
    ],
    ids=["request_room", "check_room_request", "check_room_request-404"],
)
async def test_incompatible_service(ctx, call, status, payload):
    service = _FakeService(
        status, payload, headers={api.API_VERSION_HEADER: "2"}
    )

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await call(ctx)

    assert type(caught.value) is provisioner.IncompatibleService
    assert "version '2' of its API, not '1'" in caught.value.message
    assert "'new-room' issue instead" in caught.value.message


# ---- check_room_request ----


@pytest.mark.anyio
async def test_check_room_request(ctx):
    state = {
        "room_id": ROOM,
        "state": "incomplete",
        "issue_url": ISSUE_URL,
        "incomplete": ["push the provisioned-rooms clone (down)"],
        "error": None,
    }
    service = _FakeService(200, state)

    with _patch_service(service):
        result = await provisioner.check_room_request(ctx, room_id=ROOM)

    assert result == provisioner.RoomRequestState(**state)
    [request] = service.requests
    assert request.method == "GET"
    assert str(request.url) == f"{SERVICE_URL}/rooms/{ROOM}"
    assert _claims(request)["sub"] == "phreddy-sub"


@pytest.mark.anyio
async def test_check_room_request_quotes_the_room_id(ctx):
    service = _FakeService(404, {"detail": "no request"})

    with _patch_service(service), pytest.raises(pydantic_ai.ToolFailed):
        await provisioner.check_room_request(ctx, room_id="a:b/../ok")

    [request] = service.requests
    assert request.url.raw_path == b"/rooms/a:b%2F..%2Fok"


@pytest.mark.anyio
async def test_check_room_request_unknown(ctx):
    service = _FakeService(404, {"detail": "no request"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.check_room_request(ctx, room_id=ROOM)

    assert type(caught.value) is provisioner.UnknownRequest
    assert f"no request for room {ROOM!r}" in caught.value.message


@pytest.mark.anyio
async def test_check_room_request_timed_out(ctx):
    service = _FakeService(error=_TIMEOUT)

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.check_room_request(ctx, room_id=ROOM)

    assert type(caught.value) is provisioner.ServiceTimedOut
    assert "try again shortly" in caught.value.message
    [request] = service.requests
    assert request.extensions["timeout"]["read"] == provisioner.REQUEST_TIMEOUT


@pytest.mark.anyio
async def test_check_room_request_malformed_answer(ctx):
    service = _FakeService(200, {"room_id": ROOM, "state": "lost"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.check_room_request(ctx, room_id=ROOM)

    assert type(caught.value) is provisioner.ServiceFailed
    assert "HTTP 200: malformed answer" in caught.value.message


@pytest.mark.anyio
async def test_check_room_request_service_failed(ctx):
    service = _FakeService(500, {"detail": "boom"})

    with (
        _patch_service(service),
        pytest.raises(pydantic_ai.ToolFailed) as caught,
    ):
        await provisioner.check_room_request(ctx, room_id=ROOM)

    assert type(caught.value) is provisioner.ServiceFailed
    assert "HTTP 500: boom" in caught.value.message
