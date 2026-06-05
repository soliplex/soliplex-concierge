#!/usr/bin/env python3
"""Read and resolve Soliplex room-request issues on a Gitea repository.

This is a self-contained CLI for the `soliplex-concierge-admin` skill: it runs
in an external coding agent (NOT inside the Soliplex server) and talks to the
Gitea REST API directly with httpx. It mirrors the auth and URL conventions of
the room's `create_gitea_issue` tool (`Authorization: token <token>`,
`/api/v1/repos/{owner}/{repo}/issues...`).

Configuration comes from flags or, as a fallback, the environment:

    --host  / GITEA_HOST           e.g. https://gitea.example.com
    --token / GITEA_ACCESS_TOKEN   an access token that may close issues
    --owner / --repo               the tracking repository

The admin token is typically more privileged than the room's filing token
(it must be able to comment on and close issues).

Subcommands:

    list                       list open request issues (--state, --label)
    show   <number>            print one issue's title, metadata, and body
    comment <number> --body T  add a comment recording the outcome
    close  <number> [--body T]  optionally comment, then close the issue
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx


def _issues_url(host: str, owner: str, repo: str) -> str:
    return f"{host.rstrip('/')}/api/v1/repos/{owner}/{repo}/issues"


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
    with httpx.Client() as client:
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
    with httpx.Client() as client:
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
    with httpx.Client() as client:
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
    with httpx.Client() as client:
        response = client.patch(
            f"{_issues_url(host, owner, repo)}/{number}",
            headers=_headers(token),
            json={"state": "closed"},
        )
    response.raise_for_status()
    return response.json()


def _resolve_conn(args: argparse.Namespace) -> tuple[str, str]:
    host = args.host or os.environ.get("GITEA_HOST")
    token = args.token or os.environ.get("GITEA_ACCESS_TOKEN")
    if not host or not token:
        sys.exit("error: set --host/GITEA_HOST and --token/GITEA_ACCESS_TOKEN")
    return host, token


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


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=None, help="Gitea base URL")
    common.add_argument("--token", default=None, help="Gitea access token")
    common.add_argument("--owner", required=True, help="Gitea repo owner")
    common.add_argument("--repo", required=True, help="Gitea repo name")

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser(
        "list", parents=[common], help="list request issues"
    )
    p_list.add_argument("--state", default="open", help="open/closed/all")
    p_list.add_argument(
        "--label", action="append", help="filter by label (repeatable)"
    )
    p_list.set_defaults(func=_cmd_list)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
