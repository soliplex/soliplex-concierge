"""What the provisioner's core hands its adapters, and what it asks of them.

The core (:mod:`soliplex_concierge.provisioner.provision`) runs the steps
of provisioning a room, and keeps their guarantees. Everything a step
creates, and every word it writes, comes from the deployment's adapters:

- :class:`RoomStore`: where the room's own files live, and their history
- :class:`DatabaseRegistrar`: the room's database, and how it is filled
- :class:`UploadsArea`: where the room's documents are uploaded
- :class:`Authz`: who may use the room
- :class:`Backend`: the Soliplex backend that serves the room
- :class:`IssueTracker` (optional): the request's tracking issue

A provisioner service (:mod:`soliplex_concierge.provisioner.service`) also
needs an :class:`Admission`: who is asking, and whether they may.

The core passes them data (a :class:`RoomRequest`, the tracking
:class:`Issue`, each step's outcome); they render what the deployment's
stack needs, in its own words.

Adapter conventions:

- ``clashes(request)`` describes, in the adapter's words, each thing named
  for the room that exists already; an empty result means none. It
  changes nothing; it may raise to refuse the request outright (for
  instance, a config the adapter cannot work with).
- A step that creates something returns the :class:`Undo` that removes it
  again, or None when it created nothing.
- A step after the commit (``publish``, ``reload_rooms``, ``resolve``)
  returns what remains to do, each a sentence saying how to finish it; an
  empty result means it is done.
"""

import collections.abc
import contextlib
import dataclasses
import datetime
import typing

from soliplex_concierge.provisioner import room_ids


class InvalidToken(Exception):
    """A caller's service token is not valid; the message says why.

    :meth:`Admission.claims` raises it (or a subclass).
    """


class AccessRefused(Exception):
    """The backend refuses the provisioner's own credentials.

    :meth:`Backend.check_access` raises it (or a subclass) when the backend
    answers, but refuses; any other error means it could not be asked.
    """


@dataclasses.dataclass(frozen=True)
class RoomRequest:
    """One requester's request for a provisioned room.

    ``requester_claims`` are the token claims of whoever asked; ``owner``
    is whom the room belongs to, which need not be the requester (see
    :mod:`room_ids`). ``room_id`` joins the owner part and the slug with
    the provisioner's ``room_id_separator``.
    """

    requester_claims: collections.abc.Mapping
    owner: room_ids.RoomOwner
    room_id: room_ids.RoomId
    name: str
    description: str
    requested_at: datetime.datetime

    @property
    def room(self) -> str:
        """The room id, as text."""
        return str(self.room_id)


class Issue(typing.Protocol):
    """A request's tracking issue, as its :class:`IssueTracker` filed it.

    The core reads only its ``url``; the tracker may keep anything else it
    needs on it.
    """

    url: str


@dataclasses.dataclass(frozen=True)
class Undo:
    """How to undo one step: ``description`` names it, should it fail."""

    description: str
    run: collections.abc.Callable[[], None]


@dataclasses.dataclass(frozen=True)
class Provisioned:
    """A created room: ``unfinished`` says what remains to do after it.

    ``commit`` identifies the room's recorded state in its store.
    """

    request: RoomRequest
    commit: str
    issue: Issue | None = None
    unfinished: tuple[str, ...] = ()


class RoomStore(typing.Protocol):
    """Where provisioned rooms' files live, and their history."""

    def locked(self) -> contextlib.AbstractContextManager:
        """Serialize provisioning runs: hold the store for one of them."""

    def prepare(self) -> None:
        """Make the store ready to take rooms (e.g., clone it)."""

    def has_room(self, request: RoomRequest) -> bool:
        """Does the store hold a room with the request's id already?"""

    def clashes(self, request: RoomRequest) -> collections.abc.Sequence[str]:
        """Anything else in the store named for the room."""

    def add_room(self, request: RoomRequest, issue: Issue | None) -> Undo:
        """Write the room's files, rendering each."""

    def commit(self, request: RoomRequest, issue: Issue | None) -> str:
        """Record the room (and whatever else this run wrote to the store).

        Returns an identifier for the recorded state (e.g., a commit id).
        """

    def publish(self) -> collections.abc.Sequence[str]:
        """Share what was recorded (e.g., push it)."""


class DatabaseRegistrar(typing.Protocol):
    """The room's database, and the ingester that fills it."""

    def clashes(self, request: RoomRequest) -> collections.abc.Sequence[str]:
        """Any database, or registration, named for the room."""

    def register(self, request: RoomRequest) -> Undo:
        """Create the room's database, and have it filled from now on."""


class UploadsArea(typing.Protocol):
    """Where the room's documents are uploaded, to be ingested."""

    def clashes(self, request: RoomRequest) -> collections.abc.Sequence[str]:
        """Any uploads already waiting for the room."""

    def create(self, request: RoomRequest) -> Undo | None:
        """Make the room's uploads area; None when it exists already."""


class Authz(typing.Protocol):
    """Who may use the room."""

    def clashes(self, request: RoomRequest) -> collections.abc.Sequence[str]:
        """Any authorization policy for the room already."""

    def grant_owner(self, request: RoomRequest) -> Undo:
        """Make the room private to its owner (``request.owner.json_path``)."""

    def audit(self) -> None:
        """Check the installation's rooms; raise when they are not valid."""

    def predicates(self) -> collections.abc.Collection[str]:
        """The predicates a named owner's access rule may call.

        That is, the functions the installation registers under
        ``meta.jsonpath_functions`` (see
        :func:`soliplex_concierge.provisioner.soliplex_api.registered_predicates`).
        """


class Backend(typing.Protocol):
    """The Soliplex backend that serves the room."""

    def check_access(self) -> None:
        """Raise unless the backend admits the provisioner (as an admin)."""

    def reload_rooms(
        self, request: RoomRequest
    ) -> collections.abc.Sequence[str]:
        """Have the backend load the room."""


class Admission(typing.Protocol):
    """Who is asking for a room, and whether they may.

    Each method takes the caller's bearer token: a soliplex service token
    holding the requester's claims (see :mod:`api`).
    """

    def claims(self, token: str) -> collections.abc.Mapping:
        """The token's claims; raise :class:`InvalidToken` if it is not
        valid."""

    def is_admitted(self, token: str) -> bool:
        """May the token's holder request rooms?

        The contract's rule: they are an admin, or hold WRITE on the
        reserved room id ``__provision__``.
        """

    def is_admin(self, token: str) -> bool:
        """Is the token's holder an admin of the installation?"""


class IssueTracker(typing.Protocol):
    """Where a request is tracked, as an issue."""

    def file(self, request: RoomRequest) -> Issue:
        """File the request's tracking issue."""

    def report_failure(self, issue: Issue, error: Exception) -> None:
        """Say on the issue that the room was not created, and why.

        The core ignores a failure to report.
        """

    def resolve(
        self, issue: Issue, provisioned: Provisioned
    ) -> collections.abc.Sequence[str]:
        """Say on the issue that the room was created, and close it.

        ``provisioned.unfinished`` names what the earlier steps after the
        commit left to do.
        """
