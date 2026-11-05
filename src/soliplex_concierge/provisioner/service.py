"""A room provisioner service: the REST API, served over a deployment's
adapters.

:func:`create_app` builds the service's FastAPI application from a
:class:`provision.Provisioner` (the deployment's adapters) and an
:class:`interfaces.Admission`. A deployment's wiring module builds both, and
a factory of its own returns the application, for ``uvicorn --factory``.

The application serves the contract in
:mod:`soliplex_concierge.provisioner.api`:

- ``POST /rooms`` admits a request (:func:`provision.admit_room`), answers
  ``202``, then creates the room in the background
  (:func:`provision.create_room`); an admin may name the room's owner
- ``GET /rooms/{room_id}`` reports a request's state, to its requester or
  an admin
- ``GET /ok`` answers ``200`` once the backend admits the provisioner; a
  watcher, started with the application, checks until it does

Request state lives in memory: a restart loses it, while the tracking
issue (if any) remains the record. So run one worker.
"""

import collections.abc
import contextlib
import dataclasses
import logging
import threading
import time
import typing

import fastapi

from soliplex_concierge.provisioner import access_rules
from soliplex_concierge.provisioner import api
from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import provision
from soliplex_concierge.provisioner import room_ids

# Seconds between checks of the provisioner's own backend access: while
# the backend refuses it, and while the backend cannot be asked.
ACCESS_RETRY = 15
BACKEND_RETRY = 5

TOKEN_REQUIRED = "a bearer service token is required"
TOKEN_INVALID = "the service token is not valid"
NOT_ADMITTED = "the requester may not request rooms"
NOT_AN_ADMIN = "only an admin may name a room's owner"

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The provisioner's own access
# --------------------------------------------------------------------------
class Access:
    """Whether the backend admits the provisioner's own credentials.

    ``problem`` says why not, while it does not.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._admitted = False
        self._problem = "the backend has not been checked yet"

    def state(self) -> tuple[bool, str | None]:
        with self._lock:
            return self._admitted, self._problem

    def update(self, admitted: bool, problem: str | None = None) -> None:
        with self._lock:
            self._admitted, self._problem = admitted, problem


def watch_access(
    backend: interfaces.Backend, access: Access, *, sleep=time.sleep
) -> None:
    """Check the provisioner's backend access until the backend admits it.

    Records each outcome in ``access``, and logs each new reason it is
    refused: an error when the backend refuses it
    (:class:`interfaces.AccessRefused`), a warning while the backend cannot
    be asked.
    """
    logged = None
    while True:
        try:
            backend.check_access()
        except interfaces.AccessRefused as error:
            problem, level, delay = str(error), logging.ERROR, ACCESS_RETRY
        except Exception as error:
            problem, level, delay = str(error), logging.WARNING, BACKEND_RETRY
        else:
            access.update(True)
            logger.info("the backend admits the provisioner")
            return
        access.update(False, problem)
        if problem != logged:
            logger.log(level, "%s", problem)
            logged = problem
        sleep(delay)


# --------------------------------------------------------------------------
# Request state
# --------------------------------------------------------------------------
@dataclasses.dataclass
class Request:
    """A room request's state.

    ``requester`` identifies who asked (see :func:`requester_id`).
    """

    room_id: str
    requester: str | None
    issue_url: str | None = None
    state: api.State = api.State.PENDING
    incomplete: tuple[str, ...] = ()
    error: str | None = None


class Requests:
    """The service's room requests, by room id; safe across threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[str, Request] = {}

    def get(self, room_id: str) -> Request | None:
        with self._lock:
            return self._requests.get(room_id)

    def reserve(self, room_id: str, requester: str | None) -> bool:
        """Record a pending request; False when one is pending already."""
        with self._lock:
            current = self._requests.get(room_id)
            if current is not None and current.state == api.State.PENDING:
                return False
            self._requests[room_id] = Request(
                room_id=room_id, requester=requester
            )
            return True

    def release(self, room_id: str) -> None:
        """Forget a request that was not admitted."""
        with self._lock:
            self._requests.pop(room_id, None)

    def update(self, room_id: str, **changes) -> None:
        with self._lock:
            request = self._requests[room_id]
            self._requests[room_id] = dataclasses.replace(request, **changes)


def requester_id(
    provisioner: provision.Provisioner, claims: collections.abc.Mapping
) -> str | None:
    """Who the requester is, for showing them their requests.

    Their own owner part (as :func:`room_ids.owner_from_claims` derives
    it), or None when their claims yield none.
    """
    try:
        return room_ids.owner_from_claims(
            claims,
            claim=provisioner.owner_claim,
            flatten_value=provisioner.flatten_owner,
            separator=provisioner.room_id_separator,
        ).owner
    except (room_ids.RoomIdError, access_rules.AccessRuleError):
        return None


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------
class RoomRefused(Exception):
    """A refusal naming the room it refuses (an :class:`api.Refusal`)."""

    def __init__(self, status: int, detail: str, room_id: str):
        self.status, self.detail, self.room_id = status, detail, room_id
        super().__init__(detail)


async def _room_refused(
    request: fastapi.Request, error: RoomRefused
) -> fastapi.responses.JSONResponse:
    refusal = api.Refusal(detail=error.detail, room_id=error.room_id)
    return fastapi.responses.JSONResponse(
        status_code=error.status, content=refusal.model_dump()
    )


def _http_error(status: int, error: Exception) -> fastapi.HTTPException:
    return fastapi.HTTPException(status_code=status, detail=str(error))


def _unauthorized(detail: str) -> fastapi.HTTPException:
    return fastapi.HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _provisioner(request: fastapi.Request) -> provision.Provisioner:
    return request.app.state.provisioner


def _admission(request: fastapi.Request) -> interfaces.Admission:
    return request.app.state.admission


def _requests(request: fastapi.Request) -> Requests:
    return request.app.state.requests


def _access(request: fastapi.Request) -> Access:
    return request.app.state.access


def _bearer_token(
    authorization: typing.Annotated[str | None, fastapi.Header()] = None,
) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized(TOKEN_REQUIRED)
    return token.strip()


ProvisionerDep = typing.Annotated[
    provision.Provisioner, fastapi.Depends(_provisioner)
]
AdmissionDep = typing.Annotated[
    interfaces.Admission, fastapi.Depends(_admission)
]
RequestsDep = typing.Annotated[Requests, fastapi.Depends(_requests)]
AccessDep = typing.Annotated[Access, fastapi.Depends(_access)]
TokenDep = typing.Annotated[str, fastapi.Depends(_bearer_token)]


def _claims(admission: interfaces.Admission, token: str):
    try:
        return admission.claims(token)
    except interfaces.InvalidToken as error:
        logger.info("refused a service token: %s", error)
        raise _unauthorized(TOKEN_INVALID) from error
    except Exception as error:
        logger.exception("validating a service token failed")
        raise _http_error(500, error) from error


def _check(check, token: str, what: str) -> bool:
    try:
        return check(token)
    except Exception as error:
        logger.exception("checking %s failed", what)
        raise _http_error(500, error) from error


def _named_owner(
    provisioner: provision.Provisioner, named: api.RoomOwner
) -> room_ids.RoomOwner:
    """The owner an admin names, with the access rule its form gives.

    Raises :class:`room_ids.RoomIdError` for an unusable owner part, and
    :class:`access_rules.AccessRuleError` for an unusable claim, or a
    predicate the deployment does not register.
    """
    if named.predicate is not None:
        try:
            known = provisioner.authz.predicates()
        except Exception as error:
            logger.exception("listing the registered predicates failed")
            raise _http_error(500, error) from error
        if named.predicate not in known:
            raise access_rules.UnknownPredicate(named.predicate, known)
        rule = access_rules.predicate_rule(named.predicate)
    else:
        rule = access_rules.claim_rule(named.claim, named.value)
    return room_ids.named_owner(
        named.owner, rule, separator=provisioner.room_id_separator
    )


def _create(
    provisioner: provision.Provisioner,
    requests: Requests,
    admitted: provision.Admitted,
) -> None:
    """Create the admitted room, recording the outcome."""
    room = admitted.request.room
    try:
        result = provision.create_room(admitted, provisioner)
    except Exception as error:
        logger.exception("provisioning room %r failed", room)
        requests.update(room, state=api.State.FAILED, error=str(error))
        return
    requests.update(
        room,
        state=(api.State.INCOMPLETE if result.unfinished else api.State.LIVE),
        incomplete=result.unfinished,
    )


router = fastapi.APIRouter()


@router.get(
    api.HEALTH_PATH, response_class=fastapi.responses.PlainTextResponse
)
def health_check(access: AccessDep) -> fastapi.responses.PlainTextResponse:
    """Is the service up, and does the backend admit the provisioner?

    Answers 503, saying why, until the backend does.
    """
    admitted, problem = access.state()
    if not admitted:
        return fastapi.responses.PlainTextResponse(problem, status_code=503)
    return fastapi.responses.PlainTextResponse("OK")


@router.post(api.ROOMS_PATH, status_code=202)
def post_room(
    body: api.RoomRequest,
    background: fastapi.BackgroundTasks,
    provisioner: ProvisionerDep,
    admission: AdmissionDep,
    requests: RequestsDep,
    token: TokenDep,
) -> api.Accepted:
    """Admit a room request, then create the room in the background."""
    claims = _claims(admission, token)
    if not _check(admission.is_admitted, token, "admission"):
        raise fastapi.HTTPException(status_code=403, detail=NOT_ADMITTED)
    if body.owner is not None:
        if not _check(admission.is_admin, token, "admin access"):
            raise fastapi.HTTPException(status_code=403, detail=NOT_AN_ADMIN)

    try:
        owner = (
            None
            if body.owner is None
            else _named_owner(provisioner, body.owner)
        )
        request = provision.plan_room(
            claims,
            name=body.name,
            provisioner=provisioner,
            description=body.description,
            slug=body.slug,
            owner=owner,
        )
    except (room_ids.RoomIdError, access_rules.AccessRuleError) as error:
        raise _http_error(400, error) from error

    room = request.room
    if not requests.reserve(room, requester_id(provisioner, claims)):
        raise RoomRefused(
            409, f"room {room!r} is already being provisioned", room
        )
    try:
        admitted = provision.admit_room(request, provisioner)
    except (provision.RoomExists, provision.ResourcesExist) as error:
        requests.release(room)
        raise RoomRefused(409, str(error), room) from error
    except Exception as error:
        requests.release(room)
        logger.exception("admitting room %r failed", room)
        raise _http_error(500, error) from error

    issue_url = admitted.issue.url if admitted.issue is not None else None
    requests.update(room, issue_url=issue_url)
    background.add_task(_create, provisioner, requests, admitted)
    return api.Accepted(room_id=room, issue_url=issue_url)


@router.get(api.ROOMS_PATH + "/{room_id}")
def get_room(
    room_id: str,
    provisioner: ProvisionerDep,
    admission: AdmissionDep,
    requests: RequestsDep,
    token: TokenDep,
) -> api.RequestState:
    """The state of a room request, for its requester or an admin."""
    claims = _claims(admission, token)
    request = requests.get(room_id)
    not_found = fastapi.HTTPException(
        status_code=404, detail=f"no request for room {room_id!r}"
    )
    if request is None:
        raise not_found
    caller = requester_id(provisioner, claims)
    if caller is None or caller != request.requester:
        if not _check(admission.is_admin, token, "admin access"):
            raise not_found
    return api.RequestState(
        room_id=request.room_id,
        state=request.state,
        issue_url=request.issue_url,
        incomplete=list(request.incomplete),
        error=request.error,
    )


async def _version_header(request: fastapi.Request, call_next):
    response = await call_next(request)
    response.headers[api.API_VERSION_HEADER] = api.API_VERSION
    return response


@contextlib.asynccontextmanager
async def _lifespan(app: fastapi.FastAPI):
    """Watch the provisioner's backend access while the service runs."""
    threading.Thread(
        target=watch_access,
        args=(app.state.provisioner.backend, app.state.access),
        name="provisioner-access",
        daemon=True,
    ).start()
    yield


def create_app(
    provisioner: provision.Provisioner, admission: interfaces.Admission
) -> fastapi.FastAPI:
    """The service's application, over ``provisioner`` and ``admission``.

    ``app.state.access`` records whether the backend admits the
    provisioner; the application's lifespan starts :func:`watch_access`.
    """
    app = fastapi.FastAPI(
        title="Soliplex room provisioner", lifespan=_lifespan
    )
    app.state.provisioner = provisioner
    app.state.admission = admission
    app.state.requests = Requests()
    app.state.access = Access()
    app.middleware("http")(_version_header)
    app.add_exception_handler(RoomRefused, _room_refused)
    app.include_router(router)
    return app
