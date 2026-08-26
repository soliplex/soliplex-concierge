"""Canonical Gitea request-label catalog.

Shared single source of truth for the labels the concierge uses on its
tracking repository, imported by both the room-side tool
(`soliplex_concierge.tools.gitea`, which self-creates the issue-type labels)
and the `soliplex-concierge-admin` skill's `gitea_issues.py` script (which
creates all of them via `init` and applies the decision labels). It also
carries the paging constants both sides use when they walk the repository's
label listing. This module deliberately has no imports so either side can
load it cheaply.
"""

# Gitea paginates 'GET /repos/{owner}/{repo}/labels'. Current servers apply
# their default page size even when the request asks for no page (verified on
# 1.26.0: 30 of 42 labels, with the full count only in 'X-Total-Count'; 1.22
# still returned them all), so a repository with more labels than that hides
# the ones past the first page. Both callers match labels by name, and Gitea
# accepts a second label carrying an existing name with a 201 -- a lookup that
# stops at page one therefore manufactures a duplicate instead of reusing the
# canonical label (issue #95). Walk every page instead, stopping at the first
# empty one: 'LABEL_PAGE_SIZE' is clamped server-side to Gitea's
# MAX_RESPONSE_ITEMS (50 by default), and a smaller clamp only costs extra
# requests. 'MAX_LABEL_PAGES' bounds the walk so a server that ignored 'page'
# could not spin it forever.
LABEL_PAGE_SIZE = 50
MAX_LABEL_PAGES = 20

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
