"""Unit tests for ``soliplex_concierge.audit``."""

import logging

import pytest
from soliplex import loggers

from soliplex_concierge import audit

CLAIMS = {"preferred_username": "alice", "email": "alice@example.com"}


def _records(caplog):
    return [r for r in caplog.records if r.name == "soliplex-audit"]


@pytest.fixture
def caplog_audit(caplog):
    caplog.set_level(logging.INFO, logger=loggers.SOLIPLEX_AUDIT_LOGGER_NAME)
    return caplog


def test_audited_claims_drops_the_excluded():
    result = audit.audited_claims(CLAIMS, ["email", "absent"])

    assert result == {"preferred_username": "alice"}


def test_reason():
    error = ValueError("bad slug")

    result = audit.reason(error)

    assert result == "ValueError: bad slug"


def test_reason_names_a_masked_cause():
    error = RuntimeError("the provisioner failed; reference abc")
    error.__cause__ = KeyError("no installation")

    result = audit.reason(error)

    assert result == "RuntimeError: 'no installation'"


_REQUESTS = [
    (
        "service_token_minted",
        {"purpose": "POST /rooms", "expires_in": 300},
        audit.AUDIT_SERVICE_TOKEN_MINTED,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "room_requested",
        {"status": "accepted", "room_id": "alice--notes"},
        audit.AUDIT_ROOM_REQUESTED,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "room_request_failed",
        {"reason": "BadSlug: no"},
        audit.AUDIT_ROOM_REQUESTED,
        loggers.AUDIT_OUTCOME_ERROR,
    ),
    (
        "room_request_checked",
        {"room_id": "alice--notes", "state": "live"},
        audit.AUDIT_ROOM_REQUEST_CHECKED,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "room_request_check_failed",
        {"room_id": "alice--notes", "reason": "UnknownRequest: x"},
        audit.AUDIT_ROOM_REQUEST_CHECKED,
        loggers.AUDIT_OUTCOME_ERROR,
    ),
    (
        "issue_filed",
        {
            "number": 7,
            "request_type": "new-room",
            "profile": "attached",
            "excluded_claims": ["email"],
        },
        audit.AUDIT_ISSUE_FILED,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "issue_filing_failed",
        {"request_type": "new-room", "reason": "HTTPError: 502"},
        audit.AUDIT_ISSUE_FILED,
        loggers.AUDIT_OUTCOME_ERROR,
    ),
]

_PROVISIONING = [
    (
        "service_token_refused",
        {"reason": "expired"},
        audit.AUDIT_SERVICE_TOKEN_REFUSED,
        loggers.AUDIT_OUTCOME_DENIED,
    ),
    (
        "room_request_admitted",
        {"room_id": "alice--notes", "issue_url": "https://g/i/7"},
        audit.AUDIT_ROOM_REQUEST_ADMITTED,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "room_request_refused",
        {"room_id": "alice--notes", "status": 403, "reason": "no"},
        audit.AUDIT_ROOM_REQUEST_ADMITTED,
        loggers.AUDIT_OUTCOME_DENIED,
    ),
    (
        "room_request_failed",
        {"room_id": None, "status": 500, "reason": "boom"},
        audit.AUDIT_ROOM_REQUEST_ADMITTED,
        loggers.AUDIT_OUTCOME_ERROR,
    ),
    (
        "room_request_read",
        {"room_id": "alice--notes", "as_admin": True},
        audit.AUDIT_ROOM_REQUEST_READ,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "room_request_read_denied",
        {"room_id": "alice--notes", "reason": "no such request"},
        audit.AUDIT_ROOM_REQUEST_READ,
        loggers.AUDIT_OUTCOME_DENIED,
    ),
    (
        "room_provisioned",
        {"room_id": "alice--notes", "state": "live", "unfinished": []},
        audit.AUDIT_ROOM_PROVISIONED,
        loggers.AUDIT_OUTCOME_SUCCESS,
    ),
    (
        "room_provisioning_failed",
        {"room_id": "alice--notes", "reason": "ProvisioningFailed: x"},
        audit.AUDIT_ROOM_PROVISIONED,
        loggers.AUDIT_OUTCOME_ERROR,
    ),
]


@pytest.mark.parametrize(
    "log_class, scope, method, fields, message, outcome",
    [
        (audit.RoomRequestsAuditLog, audit.AuditScopes.ROOM_REQUESTS, *case)
        for case in _REQUESTS
    ]
    + [
        (
            audit.RoomProvisioningAuditLog,
            audit.AuditScopes.ROOM_PROVISIONING,
            *case,
        )
        for case in _PROVISIONING
    ],
    ids=[f"requests-{case[0]}" for case in _REQUESTS]
    + [f"provisioning-{case[0]}" for case in _PROVISIONING],
)
def test_audit_records(
    caplog_audit, log_class, scope, method, fields, message, outcome
):
    log = log_class(claims=CLAIMS, room_id="about_x")

    getattr(log, method)(**fields)

    [record] = _records(caplog_audit)
    assert record.getMessage() == message
    assert record.levelno == (
        logging.INFO
        if outcome == loggers.AUDIT_OUTCOME_SUCCESS
        else logging.ERROR
    )
    assert getattr(record, loggers.SOLIPLEX_AUDIT_LOGGER_SCOPE_EXTRA) == scope
    assert record.outcome == outcome
    assert record.claims == CLAIMS
    assert record.room_id == fields.get("room_id", "about_x")
    for name, value in fields.items():
        assert getattr(record, name) == value
