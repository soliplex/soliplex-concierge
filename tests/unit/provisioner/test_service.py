"""Unit tests for ``soliplex_concierge.provisioner.service``.

The provisioner's adapters and the admission are fakes (``fake_parts``);
the core is the real one. Requests go through ``httpx2.ASGITransport``,
which returns once a response's background task has run.
"""

import asyncio
import logging
import re
import threading
from unittest import mock

import anyio
import fake_parts
import httpx2
import pytest

from soliplex_concierge.provisioner import api
from soliplex_concierge.provisioner import contract
from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import provision
from soliplex_concierge.provisioner import room_ids
from soliplex_concierge.provisioner import service

ROOM = "alice--q3-plans"
_SCHEME = room_ids.DEFAULT_SCHEME
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


def _assert_internal_error(response, caplog, cause):
    """A ``500`` names only a reference, which the log ties to ``cause``."""
    assert response.status_code == 500
    detail = response.json()["detail"]
    match = re.fullmatch(
        service.INTERNAL_ERROR.format(ref="(?P<ref>[0-9a-f]{12})"), detail
    )
    assert match is not None
    assert cause not in detail
    logged = [
        r for r in caplog.records if f"[ref {match['ref']}]" in r.message
    ]
    assert len(logged) == 1
    assert cause in str(logged[0].exc_info[1])


# ---- the contract ----


class _Harness:
    def __init__(self, app, script):
        self.app, self.script = app, script
        self.admitted_token = "alice-token"
        self.other_token = "bob-token"
        self.admin_token = "root-token"
        self.not_admitted_token = "carol-token"

    def occupy(self, name):
        self.script.rooms.add(
            str(room_ids.RoomId(owner="alice", slug=_SCHEME.slugify(name)))
        )


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
    room = f"{owner}--q3-plans"
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
async def test_post_room_admission_fails(app, admission, caplog, failing):
    admission.errors[failing] = RuntimeError("no verdict")

    response = await _post(app)

    _assert_internal_error(response, caplog, "no verdict")


@pytest.mark.anyio
async def test_post_room_not_admitted(app, script):
    response = await _post(app, token="carol-token")

    assert response.status_code == 403
    assert script.journal == []


STAFF = {"owner": "staff", "predicate": "is_staff"}
BHARNEY = {"owner": "bharney", "claim": "email", "value": "b@example.com"}
NIGHTS = {"owner": "Night--Shift", "predicate": "is_staff"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "owner, room, json_path",
    [
        (STAFF, "staff--q3-plans", "$[?is_staff($)]"),
        (BHARNEY, "bharney--q3-plans", '$[?$.email == "b@example.com"]'),
        (NIGHTS, "night_shift--q3-plans", "$[?is_staff($)]"),
    ],
    ids=["predicate", "claim", "separator-in-owner"],
)
async def test_post_room_admin_names_the_owner(
    app, script, owner, room, json_path
):
    response = await _post(app, token="root-token", owner=owner)

    assert response.status_code == 202
    assert response.json()["room_id"] == room
    [provisioned] = script.resolved
    assert provisioned.request.owner == room_ids.RoomOwner(
        owner=room.removesuffix("--q3-plans"), json_path=json_path
    )
    assert (
        provisioned.request.requester_claims
        == (fake_parts.TOKENS["root-token"])
    )
    assert app.state.requests.get(room).requester == "root"


@pytest.mark.anyio
async def test_post_room_non_admin_names_the_owner(app, script):
    response = await _post(app, owner=STAFF)

    assert response.status_code == 403
    assert response.json() == {"detail": service.NOT_AN_ADMIN}
    assert script.journal == []


@pytest.mark.anyio
async def test_post_room_admin_check_fails_for_a_named_owner(app, admission):
    admission.errors["is_admin"] = RuntimeError("no verdict")

    response = await _post(app, token="root-token", owner=STAFF)

    assert response.status_code == 500


@pytest.mark.anyio
@pytest.mark.parametrize(
    "owner, detail",
    [
        ({**BHARNEY, "owner": "../escape"}, "'../escape'"),
        ({**BHARNEY, "claim": "e-mail"}, "must be an identifier"),
        (
            {"owner": "x", "predicate": "is_foreman"},
            "it registers: is_staff",
        ),
    ],
    ids=["bad-owner-part", "bad-claim", "unknown-predicate"],
)
async def test_post_room_admin_names_an_unusable_owner(app, owner, detail):
    response = await _post(app, token="root-token", owner=owner)

    assert response.status_code == 400
    assert detail in response.json()["detail"]


@pytest.mark.anyio
async def test_post_room_listing_predicates_fails(app, script, caplog):
    script.failures["authz.predicates"] = RuntimeError("no installation")

    response = await _post(app, token="root-token", owner=STAFF)

    _assert_internal_error(response, caplog, "no installation")


@pytest.mark.anyio
async def test_post_room_admin_names_an_owner_with_a_raw_rule(app):
    owner = {"owner": "staff", "json_path": "$[?'staff' in @.groups]"}

    response = await _post(app, token="root-token", owner=owner)

    assert response.status_code == 422


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
async def test_post_room_refused_for_an_existing_room(app, script):
    script.rooms = {ROOM}

    response = await _post(app)

    assert response.status_code == 409
    refusal = api.Refusal.model_validate_json(response.content)
    assert refusal.room_id == ROOM
    assert "already exists" in refusal.detail
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_refused_for_existing_resources(app, script, caplog):
    script.clashes = {"uploads": ["/uploads/rooms/alice--q3-plans"]}

    response = await _post(app)

    assert response.status_code == 409
    refusal = api.Refusal.model_validate_json(response.content)
    assert refusal == api.Refusal(
        detail=service.RESOURCES_EXIST.format(room=ROOM), room_id=ROOM
    )
    assert "/uploads/rooms/alice--q3-plans" in caplog.text
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_admission_step_fails(app, script, caplog):
    script.failures["backend.check_access"] = RuntimeError("backend down")

    response = await _post(app)

    _assert_internal_error(response, caplog, "backend down")
    assert app.state.requests.get(ROOM) is None


# ---- POST /rooms: the cheap checks come first (#145) ----


def _spy(admission):
    """Wrap the admission's checks in mocks, recording their calls."""
    admission.is_admitted = mock.Mock(wraps=admission.is_admitted)
    admission.is_admin = mock.Mock(wraps=admission.is_admin)
    return admission


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token, body",
    [
        ("carol-token", {"slug": "../escape"}),
        ("carol-token", {"name": "!!!"}),
        ("bob-token", {"owner": {**BHARNEY, "owner": "../escape"}}),
        ("bob-token", {"owner": {**STAFF, "predicate": "not a name"}}),
    ],
    ids=["bad-slug", "no-slug", "bad-owner-part", "bad-predicate-name"],
)
async def test_post_room_refuses_a_malformed_request_before_admission(
    app, admission, script, token, body
):
    _spy(admission)

    response = await _post(app, token=token, **body)

    assert response.status_code == 400
    admission.is_admitted.assert_not_called()
    admission.is_admin.assert_not_called()
    assert script.journal == []
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_refuses_a_room_in_flight_before_admission(
    app, admission, script
):
    _spy(admission)
    _pending(app, "bob")

    response = await _post(app, token="alice-token")

    assert response.status_code == 409
    admission.is_admitted.assert_not_called()
    assert script.journal == []
    assert app.state.requests.get(ROOM).requester == "bob"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token, owner, status",
    [
        ("carol-token", None, 403),
        ("bob-token", STAFF, 403),
        ("root-token", {**STAFF, "predicate": "is_foreman"}, 400),
    ],
    ids=["not-admitted", "not-an-admin", "unknown-predicate"],
)
async def test_post_room_releases_the_room_when_refusing_the_caller(
    app, script, token, owner, status
):
    body = {} if owner is None else {"owner": owner}

    response = await _post(app, token=token, **body)

    assert response.status_code == status
    room = "staff--q3-plans" if owner else ROOM
    assert app.state.requests.get(room) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failing, token, owner",
    [
        ("is_admitted", "alice-token", None),
        ("is_admin", "root-token", STAFF),
    ],
)
async def test_post_room_releases_the_room_when_a_check_fails(
    app, admission, failing, token, owner
):
    admission.errors[failing] = RuntimeError("no verdict")
    body = {} if owner is None else {"owner": owner}

    response = await _post(app, token=token, **body)

    assert response.status_code == 500
    assert app.state.requests.get("staff--q3-plans" if owner else ROOM) is None


@pytest.mark.anyio
async def test_post_room_looks_predicates_up_only_for_an_admin(app, script):
    response = await _post(app, token="bob-token", owner=STAFF)

    assert response.status_code == 403
    assert "authz.predicates" not in script.journal


@pytest.mark.anyio
async def test_post_room_lists_predicates_only_to_an_admin(app):
    owner = {**STAFF, "predicate": "is_foreman"}

    response = await _post(app, token="bob-token", owner=owner)

    assert response.status_code == 403
    assert "is_staff" not in response.json()["detail"]


# ---- POST /rooms: the deployment's own check (#149) ----


def _app_with_check(script, admission, check):
    provisioner = fake_parts.make_provisioner(script, check_request=check)
    return service.create_app(provisioner, admission)


@pytest.mark.anyio
async def test_post_room_passes_the_check(script, admission):
    check = mock.Mock(return_value=None)
    app = _app_with_check(script, admission, check)

    response = await _post(app)

    assert response.status_code == 202
    [(claims, request)] = [call.args for call in check.call_args_list]
    assert claims == fake_parts.TOKENS["alice-token"]
    assert request.room == ROOM


@pytest.mark.anyio
@pytest.mark.parametrize(
    "refusal, retry_after",
    [
        (provision.RequestRefused(400, "description too long"), None),
        (provision.RequestRefused(409, "'q3-plans' is reserved"), None),
        (provision.RequestRefused(429, "at most 5 rooms"), None),
        (provision.RequestRefused(429, "slow down", retry_after=60), "60"),
    ],
    ids=["input-limit", "reserved-name", "quota", "rate-limit"],
)
async def test_post_room_refused_by_the_check(
    script, admission, refusal, retry_after
):
    _spy(admission)
    app = _app_with_check(script, admission, mock.Mock(side_effect=refusal))

    response = await _post(app)

    assert response.status_code == refusal.status
    assert response.json() == {"detail": refusal.detail}  # names no room
    assert response.headers.get("retry-after") == retry_after
    admission.is_admitted.assert_not_called()
    assert script.journal == []
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_retry_meets_the_request_in_flight(script, admission):
    """A retry of a request that got through is answered as in flight,
    before a rate limit could count it again."""
    check = mock.Mock(side_effect=provision.RequestRefused(429, "slow down"))
    app = _app_with_check(script, admission, check)
    _pending(app)

    response = await _post(app)

    assert response.status_code == 409
    refusal = api.Refusal.model_validate_json(response.content)
    assert refusal.room_id == ROOM
    check.assert_not_called()


@pytest.mark.anyio
async def test_post_room_check_fails(script, admission, caplog):
    check = mock.Mock(side_effect=RuntimeError("quota store down"))
    app = _app_with_check(script, admission, check)

    response = await _post(app)

    _assert_internal_error(response, caplog, "quota store down")
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_checks_only_a_well_formed_request(script, admission):
    check = mock.Mock(return_value=None)
    app = _app_with_check(script, admission, check)

    response = await _post(app, slug="../escape")

    assert response.status_code == 400
    check.assert_not_called()


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


# ---- POST /rooms: a busy store (#144) ----


@pytest.mark.anyio
@pytest.mark.parametrize(
    "delays, retry_after",
    [((12.5, 60.0), "13"), ((), "30")],
    ids=["first-delay", "default"],
)
async def test_post_room_store_busy(admission, caplog, delays, retry_after):
    holder = interfaces.StoreBusy("held by other-host pid 7")
    script = fake_parts.Script(lock_refusals=[holder])
    provisioner = fake_parts.make_provisioner(script, busy_retry_delays=delays)
    app = service.create_app(provisioner, admission)

    response = await _post(app)

    assert response.status_code == 503
    assert response.headers["retry-after"] == retry_after
    assert response.json() == {
        "detail": service.STORE_BUSY.format(retry_after=retry_after)
    }
    assert "other-host" in caplog.text
    assert app.state.requests.get(ROOM) is None


@pytest.mark.anyio
async def test_post_room_creation_finds_the_store_busy(script, monkeypatch):
    monkeypatch.setattr(provision.time, "sleep", mock.Mock())
    holder = interfaces.StoreBusy("held by other-host pid 7")
    script.lock_refusals = [None, holder, holder, holder]
    app = service.create_app(
        fake_parts.make_provisioner(script), fake_parts.FakeAdmission()
    )

    response = await _post(app)

    assert response.status_code == 202
    request = app.state.requests.get(ROOM)
    assert request.state == api.State.FAILED
    assert "other-host" in request.error


# ---- GET /rooms/{room_id} ----


@pytest.mark.anyio
@pytest.mark.parametrize(
    "requester, token",
    [
        ("alice", "alice-token"),
        ("alice", "root-token"),
        ("root", "root-token"),
    ],
    ids=["requester", "admin", "requester-and-admin"],
)
async def test_get_room(app, requester, token):
    _pending(app, requester)
    app.state.requests.update(ROOM, issue_url=ISSUE_URL)

    response = await _get(app, token=token)

    assert response.status_code == 200
    assert api.RequestState.model_validate_json(response.content) == (
        api.RequestState(
            room_id=ROOM, state=api.State.PENDING, issue_url=ISSUE_URL
        )
    )


@pytest.mark.anyio
async def test_get_room_needs_no_admin_check_with_nothing_to_hide(
    app, admission
):
    _pending(app)
    admission.errors["is_admin"] = RuntimeError("no verdict")

    response = await _get(app, token="alice-token")

    assert response.status_code == 200


_INCOMPLETE = {
    "state": api.State.INCOMPLETE,
    "incomplete": ("push /srv/rooms.git to gitea.internal",),
}
_FAILED = {"state": api.State.FAILED, "error": "CliFailed: Traceback ..."}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes, requester, token, incomplete, error",
    [
        (
            _INCOMPLETE,
            "alice",
            "alice-token",
            [service.SETUP_UNFINISHED],
            None,
        ),
        (_FAILED, "alice", "alice-token", [], service.CREATION_FAILED),
        (
            _INCOMPLETE,
            "alice",
            "root-token",
            ["push /srv/rooms.git to gitea.internal"],
            None,
        ),
        (_FAILED, "alice", "root-token", [], "CliFailed: Traceback ..."),
        (
            _FAILED,
            "root",
            "root-token",
            [],
            "CliFailed: Traceback ...",
        ),
    ],
    ids=[
        "incomplete-to-requester",
        "failed-to-requester",
        "incomplete-to-admin",
        "failed-to-admin",
        "failed-to-requester-and-admin",
    ],
)
async def test_get_room_detail(
    app, changes, requester, token, incomplete, error
):
    _pending(app, requester)
    app.state.requests.update(ROOM, **changes)

    response = await _get(app, token=token)

    assert response.status_code == 200
    state = api.RequestState.model_validate_json(response.content)
    assert state.state == changes["state"]
    assert state.incomplete == incomplete
    assert state.error == error


@pytest.mark.anyio
async def test_get_room_admin_check_fails_for_the_requester(
    app, admission, caplog
):
    _pending(app)
    app.state.requests.update(ROOM, **_FAILED)
    admission.errors["is_admin"] = RuntimeError("no verdict")

    response = await _get(app, token="alice-token")

    _assert_internal_error(response, caplog, "no verdict")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token", ["bob-token", "nameless-token", "Alice-token"]
)
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


@pytest.fixture
async def busy_threadpool():
    """Hold every token of the threadpool the sync routes run on."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    borrowers = [object() for _ in range(int(limiter.total_tokens))]
    for borrower in borrowers:
        limiter.acquire_on_behalf_of_nowait(borrower)
    yield limiter
    for borrower in borrowers:
        limiter.release_on_behalf_of(borrower)


@pytest.mark.anyio
async def test_health_check_answers_with_the_threadpool_busy(
    app, busy_threadpool
):
    app.state.access.update(True)

    with anyio.fail_after(5):
        response = await _send(app, "GET", api.HEALTH_PATH)

    assert busy_threadpool.available_tokens == 0
    assert response.status_code == 200
    assert response.text == "OK"


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
        ({"preferred_username": "Alice"}, {}, "Alice"),
        (
            {"email": "a@b.example"},
            {
                "owner_claim": "email",
                "scheme": room_ids.RoomIdScheme(flatten_owner=True),
            },
            "a@b.example",
        ),
        ({"preferred_username": "jo--smith"}, {}, "jo--smith"),
        (
            {"preferred_username": "jo__smith"},
            {"scheme": room_ids.RoomIdScheme(separator="__")},
            "jo__smith",
        ),
        ({"preferred_username": "Jo Smith"}, {}, "Jo Smith"),
        ({"sub": "x"}, {}, None),
        ({"preferred_username": 42}, {}, None),
        ({"x": "alice"}, {"owner_claim": "not a claim"}, None),
    ],
    ids=[
        "plain",
        "not-lowercased",
        "not-flattened",
        "separator-kept",
        "custom-separator-kept",
        "not-a-segment",
        "claim-missing",
        "claim-not-a-string",
        "bad-owner-claim",
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
