"""The room provisioner's HTTP API: the contract its implementations serve.

A room provisioner creates a private RAG room on a requesting user's
behalf. The concierge's 'request_room' and 'check_room_request' tools
(:mod:`soliplex_concierge.tools.provisioner`) are its client; each
deployment serves the API with its own implementation, since what creating
a room involves depends on the stack.

Credentials
-----------

Every request to ``/rooms`` carries a Bearer soliplex *service token*
holding the requesting user's claims, signed with the installation's
:data:`SERVICE_TOKEN_SECRET` (the caller mints it; see
``soliplex.token_mint``). The service validates it with the same secret,
and answers ``401`` (with ``WWW-Authenticate: Bearer``) when it is missing
or not valid.

Admission
---------

The service admits a requester who is an admin of the installation, or who
holds WRITE on the reserved room id :data:`ADMISSION_ROOM_ID`; it answers
any other requester ``403``.

Routes and statuses
-------------------

- ``POST /rooms`` (:data:`ROOMS_PATH`), with a :class:`RoomRequest` body:

  - ``202``: admitted; :class:`Accepted`. The room is created in the
    background: ``GET /rooms/{room_id}`` reports how that goes.
  - ``400``: the name, slug or named owner yields no usable room id;
    :class:`Error`
  - ``401``: no service token, or one that is not valid; :class:`Error`
  - ``403``: the requester is not admitted, or names an owner without
    being an admin; :class:`Error`
  - ``409``: the room exists, or a request for it is in flight;
    :class:`Refusal`, naming the room
  - ``422``: the body is not a :class:`RoomRequest`
  - ``500``: the service failed; :class:`Error`

- ``GET /rooms/{room_id}`` (:func:`room_path`):

  - ``200``: :class:`RequestState`
  - ``401``: as for ``POST /rooms``
  - ``404``: no request for that room, or one the caller may not see (only
    its requester and admins may); :class:`Error`

- ``GET /ok`` (:data:`HEALTH_PATH`), for health checks, unauthenticated:

  - ``200``: the service can create rooms (plain text ``OK``)
  - ``503``: it cannot yet (for instance, the backend does not admit the
    provisioner itself); plain text saying why

Room ids are ``<owner>:<slug>``. The room's owner is the requester, its
owner part derived from one of their claims, unless an admin names another
in the request's ``owner`` (a :class:`RoomOwner`: a group, say). The slug
is the request's ``slug``, or else derives from its ``name``.

Versions
--------

Every response carries the :data:`API_VERSION_HEADER` header, with the
:data:`API_VERSION` the service implements.
"""

import enum
import typing
import urllib.parse

import pydantic

API_VERSION = "1"
API_VERSION_HEADER = "X-Soliplex-Provisioner-API"

SERVICE_TOKEN_SECRET = "URL_SAFE_TOKEN_SECRET"
ADMISSION_ROOM_ID = "__provision__"

ROOMS_PATH = "/rooms"
HEALTH_PATH = "/ok"


def room_path(room_id: str) -> str:
    """The path of the request for room ``room_id``, quoted as one segment.

    The ``:`` separating a room id's parts stays as is.
    """
    return f"{ROOMS_PATH}/{urllib.parse.quote(room_id, safe=':')}"


class State(enum.StrEnum):
    """A room request's state, once admitted."""

    PENDING = "pending"  # the room is being created
    LIVE = "live"  # the room is ready
    INCOMPLETE = "incomplete"  # the room exists; steps remain for an admin
    FAILED = "failed"  # the room was not created


class RoomOwner(pydantic.BaseModel):
    """A room's owner, as an admin names it.

    ``owner`` is the room id's owner part; ``json_path`` is the access rule
    granting the owner the room: a soliplex ACL JSONPath over a user's
    claims.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    owner: str
    json_path: str


class RoomRequest(pydantic.BaseModel):
    """The body of ``POST /rooms``.

    ``owner``, which only an admin may give, makes someone other than the
    requester the room's owner.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    slug: str | None = None
    owner: RoomOwner | None = None


class Accepted(pydantic.BaseModel):
    """The ``202`` answer to ``POST /rooms``."""

    room_id: str
    issue_url: str | None = None


class RequestState(pydantic.BaseModel):
    """The ``200`` answer to ``GET /rooms/{room_id}``.

    ``incomplete`` lists the steps an admin must finish (state
    ``incomplete``); ``error`` says why the room was not created (state
    ``failed``).
    """

    room_id: str
    state: State
    issue_url: str | None = None
    incomplete: list[str] = []
    error: str | None = None


class Error(pydantic.BaseModel):
    """An error answer; ``detail`` is usually a message."""

    detail: typing.Any


class Refusal(Error):
    """A ``409`` answer to ``POST /rooms``, naming the room it refuses.

    The caller cannot work the room id out for itself.
    """

    detail: str
    room_id: str
