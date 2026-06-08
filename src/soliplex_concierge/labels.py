"""Canonical Gitea request-label catalog.

Shared single source of truth for the labels the concierge uses on its
tracking repository, imported by both the room-side tool
(`soliplex_concierge.tools.gitea`, which self-creates the issue-type labels)
and the `soliplex-concierge-admin` skill's `gitea_issues.py` script (which
creates all of them via `init` and applies the decision labels). This module
deliberately has no imports so either side can load it cheaply.
"""

ISSUE_TYPE_LABELS: dict[str, dict[str, str]] = {
    "new-room": {
        "color": "#1d76db",
        "description": "New room request",
    },
    "room-access": {
        "color": "#5319e7",
        "description": "Access request for an existing room",
    },
}

DECISION_LABELS: dict[str, dict[str, str]] = {
    "approved": {
        "color": "#0e8a16",
        "description": "Room request approved",
    },
    "denied": {
        "color": "#d73a4a",
        "description": "Room request denied",
    },
}

REQUEST_LABELS: dict[str, dict[str, str]] = {
    **ISSUE_TYPE_LABELS,
    **DECISION_LABELS,
}
