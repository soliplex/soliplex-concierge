"""The concierge's audit trail, on soliplex's audit logger (#152).

Records go to soliplex's ``soliplex-audit`` logger, as soliplex's own do
(see :class:`soliplex.loggers.AuditLogWrapper`), so one logging config
captures ragserver's, the room tools' and a provisioner's records alike.
Each carries an ``audit-scope``, an ``outcome`` (``success``, ``denied`` or
``error``), the acting user's ``claims``, and the event's own fields.

Two scopes:

- ``room-requests`` (:class:`RoomRequestsAuditLog`): what the room tools do
  on a user's behalf with the installation's credentials, inside the
  soliplex server: minting the user a service token, asking the provisioner
  for a room or about one, and filing a Gitea issue.

- ``room-provisioning`` (:class:`RoomProvisioningAuditLog`): what the
  provisioner service decides: refusing a token, admitting or refusing a
  request, letting someone read one, and how creating the room ended.
"""

from __future__ import annotations

import collections.abc
import enum
import typing

from soliplex import loggers


class AuditScopes(enum.StrEnum):
    ROOM_REQUESTS = "room-requests"
    ROOM_PROVISIONING = "room-provisioning"


# room-requests audit events
AUDIT_SERVICE_TOKEN_MINTED = "service token minted"
AUDIT_ROOM_REQUESTED = "room requested"
AUDIT_ROOM_REQUEST_CHECKED = "room request checked"
AUDIT_ISSUE_FILED = "issue filed"

# room-provisioning audit events
AUDIT_SERVICE_TOKEN_REFUSED = "service token refused"
AUDIT_ROOM_REQUEST_ADMITTED = "room request admitted"
AUDIT_ROOM_REQUEST_READ = "room request read"
AUDIT_ROOM_PROVISIONED = "room provisioned"


def audited_claims(
    claims: collections.abc.Mapping[str, typing.Any],
    exclude: collections.abc.Iterable[str] = (),
) -> dict[str, typing.Any]:
    """``claims``, less those a deployment keeps out of its records."""
    excluded = set(exclude)
    return {key: value for key, value in claims.items() if key not in excluded}


def reason(error: BaseException) -> str:
    """Why an action failed, for its record: the error, named, and its
    cause's text when it hides one (as a masked answer does)."""
    cause = error.__cause__
    text = str(cause) if cause is not None else str(error)
    return f"{type(error).__name__}: {text}"


class RoomRequestsAuditLog(loggers.AuditLogWrapper):
    """Record what the room tools do on a user's behalf."""

    def __init__(self, claims: dict[str, typing.Any], **extra):
        super().__init__(
            scope=AuditScopes.ROOM_REQUESTS, **({"claims": claims} | extra)
        )

    def service_token_minted(self, *, purpose: str, expires_in: int):
        self._succeeded(
            AUDIT_SERVICE_TOKEN_MINTED, purpose=purpose, expires_in=expires_in
        )

    def room_requested(self, *, status: str, room_id: str | None):
        self._succeeded(AUDIT_ROOM_REQUESTED, status=status, room_id=room_id)

    def room_request_failed(self, *, reason: str):
        self._failed(AUDIT_ROOM_REQUESTED, reason=reason)

    def room_request_checked(self, *, room_id: str, state: str):
        self._succeeded(
            AUDIT_ROOM_REQUEST_CHECKED, room_id=room_id, state=state
        )

    def room_request_check_failed(self, *, room_id: str, reason: str):
        self._failed(
            AUDIT_ROOM_REQUEST_CHECKED, room_id=room_id, reason=reason
        )

    def issue_filed(
        self,
        *,
        number: int,
        request_type: str,
        profile: str,
        excluded_claims: list[str],
    ):
        self._succeeded(
            AUDIT_ISSUE_FILED,
            number=number,
            request_type=request_type,
            profile=profile,
            excluded_claims=excluded_claims,
        )

    def issue_filing_failed(self, *, request_type: str, reason: str):
        self._failed(
            AUDIT_ISSUE_FILED, request_type=request_type, reason=reason
        )


class RoomProvisioningAuditLog(loggers.AuditLogWrapper):
    """Record what the provisioner service decides."""

    def __init__(self, claims: dict[str, typing.Any], **extra):
        super().__init__(
            scope=AuditScopes.ROOM_PROVISIONING,
            **({"claims": claims} | extra),
        )

    def service_token_refused(self, *, reason: str):
        self._denied(AUDIT_SERVICE_TOKEN_REFUSED, reason=reason)

    def room_request_admitted(self, *, room_id: str, issue_url: str | None):
        self._succeeded(
            AUDIT_ROOM_REQUEST_ADMITTED, room_id=room_id, issue_url=issue_url
        )

    def room_request_refused(
        self, *, room_id: str | None, status: int, reason: str
    ):
        self._denied(
            AUDIT_ROOM_REQUEST_ADMITTED,
            room_id=room_id,
            status=status,
            reason=reason,
        )

    def room_request_failed(
        self, *, room_id: str | None, status: int, reason: str
    ):
        self._failed(
            AUDIT_ROOM_REQUEST_ADMITTED,
            room_id=room_id,
            status=status,
            reason=reason,
        )

    def room_request_read(self, *, room_id: str, as_admin: bool):
        self._succeeded(
            AUDIT_ROOM_REQUEST_READ, room_id=room_id, as_admin=as_admin
        )

    def room_request_read_denied(self, *, room_id: str, reason: str):
        self._denied(AUDIT_ROOM_REQUEST_READ, room_id=room_id, reason=reason)

    def room_provisioned(
        self, *, room_id: str, state: str, unfinished: list[str]
    ):
        self._succeeded(
            AUDIT_ROOM_PROVISIONED,
            room_id=room_id,
            state=state,
            unfinished=unfinished,
        )

    def room_provisioning_failed(self, *, room_id: str, reason: str):
        self._failed(AUDIT_ROOM_PROVISIONED, room_id=room_id, reason=reason)
