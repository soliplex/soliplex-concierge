#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["soliplex-concierge>=0.5"]
# ///
"""Read and resolve Soliplex room-request issues on a Gitea repository.

This is a CLI for the `soliplex-concierge-admin` skill: it runs in an external
coding agent (NOT inside the Soliplex server) and talks to the Gitea REST API
directly with httpx. It mirrors the auth and URL conventions of the room's
`create_gitea_issue` tool (`Authorization: token <token>`,
`/api/v1/repos/{owner}/{repo}/issues...`) and shares its request-label catalog
(`soliplex_concierge.labels`). Run it with `uv run`, which provisions the
`soliplex-concierge` dependency (and httpx) from the inline metadata above.

Configuration comes from flags or, as a fallback, the environment:

    --host  / GITEA_HOST           e.g. https://gitea.example.com
    --token / GITEA_ACCESS_TOKEN   an access token that may close issues
    --owner / --repo               the tracking repository

The admin token is typically more privileged than the room's filing token
(it must be able to comment on and close issues).

Subcommands:

    init                       create the request labels on the repository
    list                       list open request issues (--state, --label)
    search                     filter requests by --type/--decision/--state
    show   <number>            print one issue's title, metadata, and body
    comment <number> --body T  add a comment recording the outcome
    close  <number> [--body T]  optionally comment, then close the issue
    approve <number> [--body T] label 'approved', comment, and close
    deny   <number> [--body T]  label 'denied', comment, and close
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from soliplex_concierge.labels import REQUEST_LABELS as LABELS
from soliplex_concierge.tls import httpx_verify

# httpx 'verify' value: an OS-trust-store SSLContext when 'truststore' is
# present (run with 'uv run --with truststore ...' behind an enterprise CA),
# else certifi. Built once and reused across every client below.
_VERIFY = httpx_verify()


def _issues_url(host: str, owner: str, repo: str) -> str:
    return f"{host.rstrip('/')}/api/v1/repos/{owner}/{repo}/issues"


def _labels_url(host: str, owner: str, repo: str) -> str:
    return f"{host.rstrip('/')}/api/v1/repos/{owner}/{repo}/labels"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"token {token}"}


def list_issues(
    host: str,
    token: str,
    owner: str,
    repo: str,
    state: str = "open",
    labels: list[str] | None = None,
) -> list[dict]:
    """Return issues (excluding pull requests) for the repository."""
    params: dict[str, str] = {"state": state, "type": "issues"}
    if labels:
        params["labels"] = ",".join(labels)
    with httpx.Client(verify=_VERIFY) as client:
        response = client.get(
            _issues_url(host, owner, repo),
            headers=_headers(token),
            params=params,
        )
    response.raise_for_status()
    return response.json()


def get_issue(
    host: str, token: str, owner: str, repo: str, number: int
) -> dict:
    """Return a single issue's full payload."""
    with httpx.Client(verify=_VERIFY) as client:
        response = client.get(
            f"{_issues_url(host, owner, repo)}/{number}",
            headers=_headers(token),
        )
    response.raise_for_status()
    return response.json()


def comment_issue(
    host: str, token: str, owner: str, repo: str, number: int, body: str
) -> dict:
    """Add a comment to an issue and return the created comment."""
    with httpx.Client(verify=_VERIFY) as client:
        response = client.post(
            f"{_issues_url(host, owner, repo)}/{number}/comments",
            headers=_headers(token),
            json={"body": body},
        )
    response.raise_for_status()
    return response.json()


def close_issue(
    host: str,
    token: str,
    owner: str,
    repo: str,
    number: int,
    body: str | None = None,
) -> dict:
    """Optionally comment, then mark the issue closed; return the issue."""
    if body:
        comment_issue(host, token, owner, repo, number, body)
    with httpx.Client(verify=_VERIFY) as client:
        response = client.patch(
            f"{_issues_url(host, owner, repo)}/{number}",
            headers=_headers(token),
            json={"state": "closed"},
        )
    response.raise_for_status()
    return response.json()


def list_labels(host: str, token: str, owner: str, repo: str) -> list[dict]:
    """Return the labels defined on the repository."""
    with httpx.Client(verify=_VERIFY) as client:
        response = client.get(
            _labels_url(host, owner, repo),
            headers=_headers(token),
        )
    response.raise_for_status()
    return response.json()


def create_label(
    host: str,
    token: str,
    owner: str,
    repo: str,
    name: str,
    color: str,
    description: str,
) -> dict:
    """Create a label on the repository and return it."""
    with httpx.Client(verify=_VERIFY) as client:
        response = client.post(
            _labels_url(host, owner, repo),
            headers=_headers(token),
            json={"name": name, "color": color, "description": description},
        )
    response.raise_for_status()
    return response.json()


def add_labels_to_issue(
    host: str,
    token: str,
    owner: str,
    repo: str,
    number: int,
    label_ids: list[int],
) -> list[dict]:
    """Attach the given label ids to an issue; return its labels."""
    with httpx.Client(verify=_VERIFY) as client:
        response = client.post(
            f"{_issues_url(host, owner, repo)}/{number}/labels",
            headers=_headers(token),
            json={"labels": label_ids},
        )
    response.raise_for_status()
    return response.json()


def _resolve_label_ids(
    host: str, token: str, owner: str, repo: str, names: list[str]
) -> list[int]:
    """Map label names to ids; raise if any are not defined on the repo."""
    by_name = {
        label["name"]: label["id"]
        for label in list_labels(host, token, owner, repo)
    }
    missing = [name for name in names if name not in by_name]
    if missing:
        names_repr = ", ".join(missing)
        msg = (
            f"missing label(s) {names_repr!r} on {owner}/{repo}; "
            "run 'gitea_issues.py init' to create them"
        )
        raise ValueError(msg)
    return [by_name[name] for name in names]


def init_labels(
    host: str, token: str, owner: str, repo: str
) -> dict[str, str]:
    """Create any missing request labels; return {name: created|exists}."""
    existing = {
        label["name"] for label in list_labels(host, token, owner, repo)
    }
    results: dict[str, str] = {}
    for name, spec in LABELS.items():
        if name in existing:
            results[name] = "exists"
            continue
        create_label(
            host, token, owner, repo, name, spec["color"], spec["description"]
        )
        results[name] = "created"
    return results


def _decision_comment(status: str, body: str | None) -> str:
    """Build a closing comment that records the decision and any detail."""
    line = f"**Decision: {status.upper()}**"
    return f"{line}\n\n{body}" if body else line


def approve_issue(
    host: str,
    token: str,
    owner: str,
    repo: str,
    number: int,
    body: str | None = None,
) -> dict:
    """Label an issue 'approved', record the decision, and close it."""
    ids = _resolve_label_ids(host, token, owner, repo, ["approved"])
    add_labels_to_issue(host, token, owner, repo, number, ids)
    return close_issue(
        host, token, owner, repo, number, _decision_comment("approved", body)
    )


def deny_issue(
    host: str,
    token: str,
    owner: str,
    repo: str,
    number: int,
    body: str | None = None,
) -> dict:
    """Label an issue 'denied', record the decision, and close it."""
    ids = _resolve_label_ids(host, token, owner, repo, ["denied"])
    add_labels_to_issue(host, token, owner, repo, number, ids)
    return close_issue(
        host, token, owner, repo, number, _decision_comment("denied", body)
    )


def _resolve_conn(args: argparse.Namespace) -> tuple[str, str]:
    host = args.host or os.environ.get("GITEA_HOST")
    token = args.token or os.environ.get("GITEA_ACCESS_TOKEN")
    if not host or not token:
        sys.exit("error: set --host/GITEA_HOST and --token/GITEA_ACCESS_TOKEN")
    return host, token


def _cmd_init(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    results = init_labels(host, token, args.owner, args.repo)
    for name, action in results.items():
        print(f"{name}: {action}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    issues = list_issues(
        host, token, args.owner, args.repo, args.state, args.label
    )
    if not issues:
        print(f"no {args.state} issues")
        return 0
    for issue in issues:
        print(f"#{issue['number']}\t{issue['title']}\t{issue['html_url']}")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    labels = list(args.label or [])
    if args.type:
        labels.append(args.type)
    if args.decision:
        labels.append(args.decision)
    issues = list_issues(
        host, token, args.owner, args.repo, args.state, labels or None
    )
    if not issues:
        print(f"no matching {args.state} issues")
        return 0
    for issue in issues:
        print(f"#{issue['number']}\t{issue['title']}\t{issue['html_url']}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    issue = get_issue(host, token, args.owner, args.repo, args.number)
    user = (issue.get("user") or {}).get("login", "?")
    print(f"#{issue['number']} {issue['title']}")
    print(f"state: {issue['state']}\tby: {user}\turl: {issue['html_url']}")
    print()
    print(issue.get("body") or "(no body)")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    comment = comment_issue(
        host, token, args.owner, args.repo, args.number, args.body
    )
    print(f"commented on #{args.number}: {comment['html_url']}")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    close_issue(host, token, args.owner, args.repo, args.number, args.body)
    print(f"closed #{args.number}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    approve_issue(host, token, args.owner, args.repo, args.number, args.body)
    print(f"approved #{args.number}")
    return 0


def _cmd_deny(args: argparse.Namespace) -> int:
    host, token = _resolve_conn(args)
    deny_issue(host, token, args.owner, args.repo, args.number, args.body)
    print(f"denied #{args.number}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=None, help="Gitea base URL")
    common.add_argument("--token", default=None, help="Gitea access token")
    common.add_argument("--owner", required=True, help="Gitea repo owner")
    common.add_argument("--repo", required=True, help="Gitea repo name")

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser(
        "init", parents=[common], help="create the request labels"
    )
    p_init.set_defaults(func=_cmd_init)

    p_list = subparsers.add_parser(
        "list", parents=[common], help="list request issues"
    )
    p_list.add_argument("--state", default="open", help="open/closed/all")
    p_list.add_argument(
        "--label", action="append", help="filter by label (repeatable)"
    )
    p_list.set_defaults(func=_cmd_list)

    p_search = subparsers.add_parser(
        "search", parents=[common], help="filter requests by label"
    )
    p_search.add_argument(
        "--type",
        choices=["new-room", "room-access"],
        help="filter by issue-type label",
    )
    p_search.add_argument(
        "--decision",
        choices=["approved", "denied"],
        help="filter by decision label",
    )
    p_search.add_argument("--state", default="open", help="open/closed/all")
    p_search.add_argument(
        "--label", action="append", help="extra label filter (repeatable)"
    )
    p_search.set_defaults(func=_cmd_search)

    p_show = subparsers.add_parser(
        "show", parents=[common], help="show one issue"
    )
    p_show.add_argument("number", type=int)
    p_show.set_defaults(func=_cmd_show)

    p_comment = subparsers.add_parser(
        "comment", parents=[common], help="comment on an issue"
    )
    p_comment.add_argument("number", type=int)
    p_comment.add_argument("--body", required=True, help="comment text")
    p_comment.set_defaults(func=_cmd_comment)

    p_close = subparsers.add_parser(
        "close", parents=[common], help="close (resolve) an issue"
    )
    p_close.add_argument("number", type=int)
    p_close.add_argument(
        "--body", default=None, help="optional closing comment"
    )
    p_close.set_defaults(func=_cmd_close)

    p_approve = subparsers.add_parser(
        "approve", parents=[common], help="approve and close a request"
    )
    p_approve.add_argument("number", type=int)
    p_approve.add_argument(
        "--body", default=None, help="optional decision detail"
    )
    p_approve.set_defaults(func=_cmd_approve)

    p_deny = subparsers.add_parser(
        "deny", parents=[common], help="deny and close a request"
    )
    p_deny.add_argument("number", type=int)
    p_deny.add_argument(
        "--body", default=None, help="optional decision detail"
    )
    p_deny.set_defaults(func=_cmd_deny)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
