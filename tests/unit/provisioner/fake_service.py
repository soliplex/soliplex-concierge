"""A minimal room provisioner serving the API, for testing the contract.

Each of ``create_app``'s ``faults`` breaks one part of the contract, so
the tests can show that the checks catch it.
"""

import dataclasses
import re
import typing

import fastapi
import pydantic

from soliplex_concierge.provisioner import api

# token -> (owner part, role)
USERS = {
    "alice-token": ("alice", "admitted"),
    "bob-token": ("bob", "admitted"),
    "root-token": ("root", "admin"),
    "carol-token": ("carol", "unadmitted"),
}
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

FAULTS = frozenset(
    {
        "no-version-header",  # answers without the version header
        "unhealthy",  # GET /ok answers 503
        "no-auth",  # takes a request with no token for alice's
        "no-www-authenticate",  # 401 without 'WWW-Authenticate'
        "trust-any-token",  # takes an invalid token for alice's
        "admit-everyone",  # admits the unadmitted
        "lenient-body",  # ignores unknown fields
        "accept-any-slug",  # accepts unusable slugs
        "accept-with-200",  # accepts with 200, not 202
        "malformed-accepted",  # accepts with a body that is no Accepted
        "wrong-room-id",  # reports a request under another room id
        "show-others",  # shows a request to another requester
        "hide-from-admins",  # hides a request from admins
        "unknown-as-pending",  # reports unknown requests as pending
        "overwrite",  # accepts a request for an existing room
        "anyone-names-owners",  # lets a non-admin name an owner
        "accept-any-owner",  # accepts unusable owner parts
        "ignore-named-owner",  # gives an admin's room to the admin
    }
)


class _LenientRequest(pydantic.BaseModel):
    name: str
    description: str | None = None
    slug: str | None = None
    owner: api.RoomOwner | None = None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def create_app(faults: frozenset[str] = frozenset()) -> fastapi.FastAPI:
    assert faults <= FAULTS, faults - FAULTS
    app = fastapi.FastAPI()
    app.state.rooms = set()
    requests: dict[str, tuple[str, api.RequestState]] = {}

    @app.middleware("http")
    async def version_header(request, call_next):
        response = await call_next(request)
        if "no-version-header" not in faults:
            response.headers[api.API_VERSION_HEADER] = api.API_VERSION
        return response

    def requester(
        authorization: typing.Annotated[str | None, fastapi.Header()] = None,
    ) -> tuple[str, str]:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme != "Bearer":
            if "no-auth" in faults:
                return USERS["alice-token"]
            raise fastapi.HTTPException(
                status_code=401,
                detail="a bearer service token is required",
                headers=(
                    {}
                    if "no-www-authenticate" in faults
                    else {"WWW-Authenticate": "Bearer"}
                ),
            )
        if token not in USERS:
            if "trust-any-token" in faults:
                return USERS["alice-token"]
            raise fastapi.HTTPException(
                status_code=401,
                detail="the service token is not valid",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return USERS[token]

    Requester = typing.Annotated[tuple[str, str], fastapi.Depends(requester)]
    Body = _LenientRequest if "lenient-body" in faults else api.RoomRequest

    plain_text = fastapi.responses.PlainTextResponse

    @app.get(api.HEALTH_PATH, response_class=plain_text)
    def health_check():
        if "unhealthy" in faults:
            return plain_text(
                "the backend does not admit the provisioner", status_code=503
            )
        return "OK"

    @app.post(
        api.ROOMS_PATH,
        status_code=200 if "accept-with-200" in faults else 202,
    )
    def post_room(body: Body, who: Requester):
        owner, role = who
        if role == "unadmitted" and "admit-everyone" not in faults:
            raise fastapi.HTTPException(
                status_code=403, detail="the requester may not request rooms"
            )
        if body.owner is not None:
            if role != "admin" and "anyone-names-owners" not in faults:
                raise fastapi.HTTPException(
                    status_code=403, detail="only an admin may name owners"
                )
            named = body.owner.owner
            if not _SLUG_RE.fullmatch(named):
                if "accept-any-owner" not in faults:
                    raise fastapi.HTTPException(
                        status_code=400, detail=f"unusable owner {named!r}"
                    )
            if "ignore-named-owner" not in faults:
                owner = named
        slug = body.slug if body.slug is not None else slugify(body.name)
        if not _SLUG_RE.fullmatch(slug) and "accept-any-slug" not in faults:
            raise fastapi.HTTPException(
                status_code=400, detail=f"unusable slug {slug!r}"
            )
        room_id = f"{owner}:{slug}"
        if room_id in app.state.rooms and "overwrite" not in faults:
            refusal = api.Refusal(
                detail=f"room {room_id!r} exists", room_id=room_id
            )
            return fastapi.responses.JSONResponse(
                status_code=409, content=refusal.model_dump()
            )
        app.state.rooms.add(room_id)
        requests[room_id] = (
            owner,
            api.RequestState(room_id=room_id, state=api.State.LIVE),
        )
        if "malformed-accepted" in faults:
            return fastapi.responses.JSONResponse(
                status_code=202, content={"room": room_id}
            )
        return api.Accepted(room_id=room_id)

    @app.get(api.ROOMS_PATH + "/{room_id}")
    def get_room(room_id: str, who: Requester):
        owner, role = who
        entry = requests.get(room_id)
        if entry is None:
            if "unknown-as-pending" in faults:
                return api.RequestState(
                    room_id=room_id, state=api.State.PENDING
                )
            raise fastapi.HTTPException(
                status_code=404, detail=f"no request for room {room_id!r}"
            )
        request_owner, state = entry
        admin = role == "admin" and "hide-from-admins" not in faults
        if owner != request_owner and not admin:
            if "show-others" not in faults:
                raise fastapi.HTTPException(
                    status_code=404,
                    detail=f"no request for room {room_id!r}",
                )
        if "wrong-room-id" in faults:
            return state.model_copy(update={"room_id": "someone:else"})
        return state

    return app


@dataclasses.dataclass
class FakeHarness:
    """The contract's ``Harness``, for an app from ``create_app``."""

    app: fastapi.FastAPI
    admitted_token: str = "alice-token"
    other_token: str = "bob-token"
    admin_token: str = "root-token"
    not_admitted_token: str = "carol-token"

    def occupy(self, name: str) -> None:
        self.app.state.rooms.add(f"alice:{slugify(name)}")
