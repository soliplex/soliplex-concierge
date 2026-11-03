"""Unit tests for ``soliplex_concierge.provisioner.service``.

The provisioner's adapters and the admission are fakes (``fake_parts``);
the core is the real one. Requests go through ``httpx2.ASGITransport``,
which returns once a response's background task has run.
"""

import asyncio
import logging
import threading

import fake_parts
import httpx2
import pytest

from soliplex_concierge.provisioner import api
from soliplex_concierge.provisioner import contract
from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import provision
from soliplex_concierge.provisioner import room_ids
from soliplex_concierge.provisioner import service

ROOM = "alice:q3-plans"
ISSUE_URL = fake_parts.FakeIssue().url


@pytest.fixture
def script():
    return fake_parts.Script()


@pytest.fixture
def admission():
    return fake_parts.FakeAdmission()


@pytest.fixture
def app(script, admission):
    return service.create_app(fake_parts.make_provisioner(script), admission)


async def _send(app, method, path, *, token=None, body=None, headers=None):
    headers = dict(headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://provisioner"
    ) as client:
        return await client.request(method, path, headers=headers, json=body)


def _post(app, token="alice-token", **body):
    body.setdefault("name", "Q3 Plans")
    return _send(app, "POST", api.ROOMS_PATH, token=token, body=body)


def _get(app, room_id=ROOM, token="alice-token"):
    return _send(app, "GET", api.room_path(room_id), token=token)


def _pending(app, requester="alice"):
    app.state.requests.reserve(ROOM, requester)


# ---- the contract ----


class _Harness:
    def __init__(self, app, script):
        self.app, self.script = app, script
        self.admitted_token = "alice-token"
        self.other_token = "bob-token"
        self.admin_token = "root-token"
        self.not_admitted_token = "carol-token"

    def occupy(self, name):
        self.script.rooms.add(f"alice:{room_ids.slugify(name)}")


@pytest.mark.anyio
async def test_keeps_the_contract(app, script):
    app.state.access.update(True)
    harness = _Harness(app, script)

    result = await contract.check_all(harness)

    assert result is None


# ---- POST /rooms ----


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["alice-token", "root-token"])
async def test_post_room(app, script, token):
    response = await _post(app, token=token, description="Plans for Q3")

    assert response.status_code == 202
    assert response.headers[api.API_VERSION_HEADER] == api.API_VERSION
    owner = "alice" if token == "alice-token" else "root"
    room = f"{owner}:q3-plans"
    assert api.Accepted.model_validate_json(response.content) == (
        api.Accepted(room_id=room, issue_url=ISSUE_URL)
    )
    request = app.state.requests.get(room)
    assert request.state == api.State.LIVE
    assert request.requester == owner
    assert script.journal[-1] == "tracker.resolve"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic YWxpY2U="}, {"Authorization": "Bearer  "}],
    ids=["none", "basic", "empty"],
)
async def test_post_room_without_a_bearer_token(app, headers):
    response = await _send(
        app, "POST", api.ROOMS_PATH, body={"name": "x"}, headers=headers
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers[api.API_VERSION_HEADER] == api.API_VERSION


@pytest.mark.anyio
async def test_post_room_with_an_invalid_token(app, caplog):
    caplog.set_level(logging.INFO, logger=service.__name__)

    response = await _post(app, token="forged-token")

    assert response.status_code == 401
    assert response.json() == {"detail": "the service token is not valid"}
    assert "unknown token 'forged-token'" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("failing", ["claims", "is_admitted"])
async def test_post_room_admission_fails(app, admission, failing):
    admission.errors[failing] = RuntimeError("no verdict")

    response = await _post(app)

    assert response.status_code == 500
    assert response.json() == {"detail": "no verdict"}


@pytest.mark.anyio
async def test_post_room_not_admitted(app, script):
    response = await _post(app, token="carol-token")

    assert response.status_code == 403
    assert script.journal == []


@pytest.mark.anyio
async def test_post_room_bad_slug(app):
    response = await _post(app, slug="../escape")

    assert response.status_code == 400
    assert "'../escape'" in response.json()["detail"]


@pytest.mark.anyio
async def test_post_room_already_pending(app, script):
    _pending(app)

    response = await _post(app)

    assert response.status_code == 409
    assert api.Refusal.model_validate_json(response.content) == api.Refusal(
        detail=f"room {ROOM!r} is already being provisioned", room_id=ROOM
    )
    assert script.journal == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "setting, expected",
    [
        ({"rooms": {ROOM}}, "already exists"),
        ({"clashes": {"uploads": ["3 uploads"]}}, "3 uploads"),
    ],
    ids=["room-exists", "resources-exist"],
)
async def test_post_room_refused_at_admission(app, script, setting, expected):
    for name, value in setting.items():
        setattr(script, name, value)

    response = await _post(app)

    assert response.status_code == 409
    refusal = api.Refusal.model_validate_json(response.content)
    assert refusal.room_id == ROOM
    assert expected in refusal.detail
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_admission_step_fails(app, script):
    script.failures["backend.check_access"] = RuntimeError("backend down")

    response = await _post(app)

    assert response.status_code == 500
    assert response.json() == {"detail": "backend down"}
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_creation_fails(app, script):
    script.failures["authz.audit"] = RuntimeError("audit failed")

    response = await _post(app)

    assert response.status_code == 202
    request = app.state.requests.get(ROOM)
    assert request.state == api.State.FAILED
    assert "audit failed; rolled back" in request.error


@pytest.mark.anyio
async def test_post_room_creation_incomplete(app, script):
    script.unfinished["store.publish"] = ["push the rooms repository"]

    response = await _post(app)

    assert response.status_code == 202
    request = app.state.requests.get(ROOM)
    assert request.state == api.State.INCOMPLETE
    assert request.incomplete == ("push the rooms repository",)


# ---- GET /rooms/{room_id} ----


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["alice-token", "root-token"])
async def test_get_room(app, token):
    _pending(app)
    app.state.requests.update(
        ROOM, issue_url=ISSUE_URL, incomplete=("close the issue",)
    )

    response = await _get(app, token=token)

    assert response.status_code == 200
    assert api.RequestState.model_validate_json(response.content) == (
        api.RequestState(
            room_id=ROOM,
            state=api.State.PENDING,
            issue_url=ISSUE_URL,
            incomplete=["close the issue"],
        )
    )


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["bob-token", "nameless-token"])
async def test_get_room_by_someone_else(app, token):
    _pending(app)

    response = await _get(app, token=token)

    assert response.status_code == 404


@pytest.mark.anyio
async def test_get_room_admin_check_fails(app, admission):
    _pending(app)
    admission.errors["is_admin"] = RuntimeError("no verdict")

    response = await _get(app, token="bob-token")

    assert response.status_code == 500


@pytest.mark.anyio
async def test_get_room_unknown(app):
    response = await _get(app)

    assert response.status_code == 404
    assert response.json() == {"detail": f"no request for room {ROOM!r}"}


@pytest.mark.anyio
async def test_get_room_with_an_invalid_token(app):
    _pending(app)

    response = await _get(app, token="forged-token")

    assert response.status_code == 401


# ---- GET /ok ----


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state, status, text",
    [
        (None, 503, "the backend has not been checked yet"),
        ((False, "the backend refused"), 503, "the backend refused"),
        ((True,), 200, "OK"),
    ],
    ids=["unchecked", "refused", "admitted"],
)
async def test_health_check(app, state, status, text):
    if state is not None:
        app.state.access.update(*state)

    response = await _send(app, "GET", api.HEALTH_PATH)

    assert response.status_code == status
    assert response.text == text
    assert response.headers[api.API_VERSION_HEADER] == api.API_VERSION


# ---- watch_access ----


class _Refused(interfaces.AccessRefused):
    pass


def test_watch_access_admitted_at_once(script):
    access = service.Access()
    sleeps = []

    result = service.watch_access(
        fake_parts.FakeBackend(script), access, sleep=sleeps.append
    )

    assert result is None
    assert access.state() == (True, None)
    assert sleeps == []


def test_watch_access_until_admitted(script, caplog):
    script.access_refusals = [
        _Refused("refused: add the admin_users entry"),
        _Refused("refused: add the admin_users entry"),
        RuntimeError("backend unreachable"),
    ]
    access = service.Access()
    sleeps, states = [], []

    def sleep(delay):
        sleeps.append(delay)
        states.append(access.state())

    with caplog.at_level(logging.INFO, logger=service.__name__):
        service.watch_access(
            fake_parts.FakeBackend(script), access, sleep=sleep
        )

    assert sleeps == [
        service.ACCESS_RETRY,
        service.ACCESS_RETRY,
        service.BACKEND_RETRY,
    ]
    assert states == [
        (False, "refused: add the admin_users entry"),
        (False, "refused: add the admin_users entry"),
        (False, "backend unreachable"),
    ]
    assert access.state() == (True, None)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.ERROR, "refused: add the admin_users entry"),
        (logging.WARNING, "backend unreachable"),
        (logging.INFO, "the backend admits the provisioner"),
    ]


@pytest.mark.anyio
async def test_lifespan_watches_access(app, monkeypatch):
    watched = []
    started = threading.Event()

    def watch(backend, access):
        watched.append((backend, access))
        started.set()

    monkeypatch.setattr(service, "watch_access", watch)

    async with app.router.lifespan_context(app):
        await asyncio.to_thread(started.wait, 5)

    assert watched == [(app.state.provisioner.backend, app.state.access)]


# ---- pieces ----


def test_requests_reserve_replaces_a_finished_request():
    requests = service.Requests()
    requests.reserve(ROOM, "alice")
    requests.update(ROOM, state=api.State.FAILED, error="boom")

    result = requests.reserve(ROOM, "alice")

    assert result is True
    assert requests.get(ROOM) == service.Request(
        room_id=ROOM, requester="alice"
    )


def test_requests_release():
    requests = service.Requests()
    requests.reserve(ROOM, "alice")

    requests.release(ROOM)

    assert requests.get(ROOM) is None


@pytest.mark.parametrize(
    "claims, kwargs, expected",
    [
        ({"preferred_username": "alice"}, {}, "alice"),
        (
            {"email": "a@b.example"},
            {"owner_claim": "email", "flatten_owner": True},
            "a_b.example",
        ),
        ({"sub": "x"}, {}, None),
    ],
)
def test_requester_id(claims, kwargs, expected):
    provisioner = fake_parts.make_provisioner(fake_parts.Script(), **kwargs)

    result = service.requester_id(provisioner, claims)

    assert result == expected


def test_create_app():
    provisioner = fake_parts.make_provisioner(fake_parts.Script())
    admission = fake_parts.FakeAdmission()

    result = service.create_app(provisioner, admission)

    assert result.state.provisioner is provisioner
    assert result.state.admission is admission
    assert result.state.access.state() == (
        False,
        "the backend has not been checked yet",
    )
    assert isinstance(result.state.requests, service.Requests)
    assert isinstance(provisioner, provision.Provisioner)
