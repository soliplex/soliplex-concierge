"""Provision a private RAG room for a requester: the steps, and their order.

The core runs the steps; a deployment's adapters (see
:mod:`soliplex_concierge.provisioner.interfaces`), gathered in a
:class:`Provisioner`, do each step's work and write all its text. There
are three phases:

- planning (:func:`plan_room`): works out the room's owner (the
  requester's own, unless named) and id, changing nothing
- admission (:func:`admit_room`):

  1. checks that the backend admits the provisioner
  2. holding the store, prepares it, and refuses a room whose id anything
     uses already (:func:`check_free`)
  3. files the tracking issue, when there is a tracker

- creation (:func:`create_room`), holding the store again:

  4. checks again that the id is free
  5. writes the room's files into the store
  6. creates its uploads area
  7. registers its database
  8. makes it private to the requester
  9. audits the installation's rooms
  10. commits the room, then publishes the store

  then, without the store:

  11. has the backend load the room
  12. resolves the tracking issue

Admission and creation each hold the store for themselves, so another run
may hold it between them: hence creation's second check.

The guarantees:

- every refusal comes before anything is created
- a failure before the commit undoes what this run created, and only that
  (newest first, each undo running even when another fails), then reports
  on the tracking issue, which stays open
- after the commit the room stands as committed: a step that fails is
  reported, with how to finish it, rather than undone
"""

import collections.abc
import contextlib
import dataclasses
import datetime

from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import room_ids


class ProvisionError(Exception):
    """A refusal or failure to report to the requester."""


class BadRoomName(room_ids.RoomIdError):
    def __init__(self, name):
        self.name = name
        super().__init__(f"room name {name!r} must not be blank")


class RoomExists(ProvisionError):
    def __init__(self, room_id):
        self.room_id = room_id
        super().__init__(
            f"room {room_id!r} already exists; choose another name"
        )


class ResourcesExist(ProvisionError):
    """Things named for the room exist already, so it is not created.

    ``found`` holds the adapters' descriptions of them; an operator decides
    whether to remove them before the room is provisioned again.
    """

    def __init__(self, room_id, found):
        self.room_id, self.found = room_id, tuple(found)
        super().__init__(
            f"room {room_id!r} was not provisioned: resources named for it "
            f"already exist: {'; '.join(self.found)}. Remove them, or choose "
            "another name"
        )


class ProvisioningFailed(ProvisionError):
    """A step before the commit failed; what was done has been undone.

    ``error`` is the step's; ``rollback_failures`` describes each undo
    that itself failed, leaving its part for an operator to clean up.
    """

    def __init__(self, room_id, error, rollback_failures=()):
        self.room_id, self.error = room_id, error
        self.rollback_failures = tuple(rollback_failures)
        message = f"provisioning room {room_id!r} failed: {error}"
        if self.rollback_failures:
            message += "; rolling back, these steps failed: " + "; ".join(
                self.rollback_failures
            )
        else:
            message += "; rolled back"
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class Provisioner:
    """A deployment's adapters, and how it derives a room's owner.

    ``owner_claim`` names the requester's claim a room's owner part comes
    from; ``flatten_owner`` flattens it (see :func:`room_ids.flatten`).
    """

    store: interfaces.RoomStore
    registrar: interfaces.DatabaseRegistrar
    uploads: interfaces.UploadsArea
    authz: interfaces.Authz
    backend: interfaces.Backend
    tracker: interfaces.IssueTracker | None = None
    owner_claim: str = room_ids.DEFAULT_OWNER_CLAIM
    flatten_owner: bool = False


@dataclasses.dataclass(frozen=True)
class Admitted:
    """An admitted request, with its tracking issue (if filed)."""

    request: interfaces.RoomRequest
    issue: interfaces.Issue | None = None


def plan_room(
    requester_claims: collections.abc.Mapping,
    *,
    name: str,
    provisioner: Provisioner,
    description: str | None = None,
    slug: str | None = None,
    owner: room_ids.RoomOwner | None = None,
    now: datetime.datetime | None = None,
) -> interfaces.RoomRequest:
    """Work out the room a requester (``requester_claims``) asks for.

    ``name`` is the room's human-readable name; ``slug`` (derived from it
    when omitted) completes the room id ``<owner>:<slug>``; ``description``
    defaults to the name. ``owner`` (see :func:`room_ids.named_owner`)
    makes someone other than the requester the room's owner; whether the
    requester may do that is for the caller to decide. Without it, the
    owner is the requester, derived from their claims. Changes nothing.
    Raises :class:`room_ids.RoomIdError` for an unusable owner, name or
    slug.
    """
    if not name.strip():
        raise BadRoomName(name)
    if owner is None:
        owner = room_ids.owner_from_claims(
            requester_claims,
            claim=provisioner.owner_claim,
            flatten_value=provisioner.flatten_owner,
        )
    room_id = room_ids.make_room_id(
        owner, slug if slug is not None else room_ids.slugify(name)
    )
    return interfaces.RoomRequest(
        requester_claims=requester_claims,
        owner=owner,
        room_id=room_id,
        name=name,
        description=name if description is None else description,
        requested_at=now or datetime.datetime.now(datetime.UTC),
    )


def check_free(
    provisioner: Provisioner, request: interfaces.RoomRequest
) -> None:
    """Refuse the room when its id, or anything named for it, is in use.

    Raises :class:`RoomExists` when the store holds the room, and
    :class:`ResourcesExist` with every adapter's clashes. Changes nothing.
    """
    if provisioner.store.has_room(request):
        raise RoomExists(request.room)
    found = [
        *provisioner.store.clashes(request),
        *provisioner.registrar.clashes(request),
        *provisioner.uploads.clashes(request),
        *provisioner.authz.clashes(request),
    ]
    if found:
        raise ResourcesExist(request.room, found)


def admit_room(
    request: interfaces.RoomRequest, provisioner: Provisioner
) -> Admitted:
    """Admit ``request``: check that its room can be created; file its issue.

    Raises what :meth:`interfaces.Backend.check_access` does before holding
    the store, what :func:`check_free` does, and what
    :meth:`interfaces.IssueTracker.file` does.
    """
    provisioner.backend.check_access()

    with provisioner.store.locked():
        provisioner.store.prepare()
        check_free(provisioner, request)
        if provisioner.tracker is None:
            return Admitted(request=request)
        issue = provisioner.tracker.file(request)

    return Admitted(request=request, issue=issue)


def _report_failure(
    provisioner: Provisioner, admitted: Admitted, error: Exception
) -> None:
    if admitted.issue is not None:
        with contextlib.suppress(Exception):
            provisioner.tracker.report_failure(admitted.issue, error)


def _rollback(undos: list[interfaces.Undo]) -> list[str]:
    failures = []
    for undo in reversed(undos):
        try:
            undo.run()
        except Exception as error:
            failures.append(f"{undo.description} ({error})")
    return failures


def _after_commit(
    step: collections.abc.Callable[[], collections.abc.Sequence[str]],
) -> list[str]:
    """Run a step after the commit; return what it leaves to do.

    A step that raises leaves its error to resolve.
    """
    try:
        return list(step())
    except Exception as error:
        return [str(error)]


def create_room(
    admitted: Admitted, provisioner: Provisioner
) -> interfaces.Provisioned:
    """Create the room ``admitted`` admitted.

    Raises what :func:`check_free` does (after reporting on the tracking
    issue), and :class:`ProvisioningFailed` (after rolling back, and
    reporting) when a step before the commit fails. Steps after it that
    leave something to do are named in the result's ``unfinished``.
    """
    request, issue = admitted.request, admitted.issue
    store = provisioner.store

    with store.locked():
        try:
            check_free(provisioner, request)
        except ProvisionError as error:
            _report_failure(provisioner, admitted, error)
            raise

        undos = []
        try:
            undos.append(store.add_room(request, issue))
            uploads_undo = provisioner.uploads.create(request)
            if uploads_undo is not None:
                undos.append(uploads_undo)
            undos.append(provisioner.registrar.register(request))
            undos.append(provisioner.authz.grant_owner(request))
            provisioner.authz.audit()
            commit = store.commit(request, issue)
        except Exception as error:
            failed = ProvisioningFailed(request.room, error, _rollback(undos))
            _report_failure(provisioner, admitted, failed)
            raise failed from error

        unfinished = _after_commit(store.publish)

    unfinished += _after_commit(
        lambda: provisioner.backend.reload_rooms(request)
    )
    provisioned = interfaces.Provisioned(
        request=request,
        commit=commit,
        issue=issue,
        unfinished=tuple(unfinished),
    )
    if issue is not None:
        unfinished += _after_commit(
            lambda: provisioner.tracker.resolve(issue, provisioned)
        )
    return dataclasses.replace(provisioned, unfinished=tuple(unfinished))


def provision_room(
    requester_claims: collections.abc.Mapping,
    *,
    name: str,
    provisioner: Provisioner,
    description: str | None = None,
    slug: str | None = None,
    owner: room_ids.RoomOwner | None = None,
    now: datetime.datetime | None = None,
) -> interfaces.Provisioned:
    """Plan, admit and create a room, raising as each phase does."""
    request = plan_room(
        requester_claims,
        name=name,
        provisioner=provisioner,
        description=description,
        slug=slug,
        owner=owner,
        now=now,
    )
    return create_room(admit_room(request, provisioner), provisioner)
