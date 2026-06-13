# Administering requests

Requests filed by the [concierge](../rooms/index.md) land as issues on the
tracking repository. Administrators resolve them with the
**`soliplex-concierge-admin`** skill, run from an external coding agent. The
skill's `scripts/gitea_issues.py` CLI lists and filters the requests, lets you
follow up on an issue, and closes it with a comment and a resolution label. The
actual provisioning is delegated to the `soliplex-template` and `soliplex-docs`
skills — see [Room creation](room_creation.md) and [Room access](room_access.md).

## The `gitea_issues.py` CLI

`scripts/gitea_issues.py` is a thin shim over `soliplex_concierge.gitea_admin`
that talks to the Gitea REST API directly. The
[installer](../rooms/setup.md) also drops an identical copy into the stack at
`<stack>/scripts/gitea_issues.py`, so you can run the same CLI from the deployed
stack directory. It is a [PEP 723](https://peps.python.org/pep-0723/) script:
run it with `uv run`, which provisions its `soliplex-concierge` (and `httpx`)
dependency automatically — it only needs `uv` on the `PATH`.

Provide the Gitea connection via flags or the environment:

- `--host` / `GITEA_HOST` — the Gitea base URL.
- `--token` / `GITEA_ACCESS_TOKEN` — a token that can comment on and close
  issues (an admin token, usually more privileged than the room's filing token).
- `--owner` / `--repo` — the tracking repository.

For an enterprise / internal CA, add `--with truststore` to the `uv run`
invocation so the script verifies against the OS trust store — e.g.
`uv run --with truststore scripts/gitea_issues.py list …` (issue #46).

## Labels

The concierge tags each request with an **issue-type** label; the admin records
the outcome with a **decision** label:

| Label | Kind | Meaning |
| --- | --- | --- |
| `new-room` | issue type | New room request |
| `room-access` | issue type | Access request for an existing room |
| `approved` | decision | Room request approved |
| `denied` | decision | Room request denied |

Run `init` **once per tracking repository** to create them (idempotent —
re-running reports each as `created` or `exists`):

```sh
uv run scripts/gitea_issues.py init --owner <owner> --repo <repo>
```

## Listing and filtering

`list` shows request issues; by default it lists open ones. Use `--state`
(`open` / `closed` / `all`) and repeatable `--label` filters:

```sh
uv run scripts/gitea_issues.py list --owner <owner> --repo <repo>
uv run scripts/gitea_issues.py list --state all --label new-room \
    --owner <owner> --repo <repo>
```

`search` filters by request type and decision status directly:

```sh
# open new-room requests not yet decided
uv run scripts/gitea_issues.py search --type new-room \
    --owner <owner> --repo <repo>

# previously approved access requests (closed)
uv run scripts/gitea_issues.py search --type room-access \
    --decision approved --state closed --owner <owner> --repo <repo>
```

`--type` accepts `new-room` / `room-access`; `--decision` accepts `approved` /
`denied`; `--state` and `--label` work as for `list`.

## Following up on an issue

Read the full request — title, metadata, and body:

```sh
uv run scripts/gitea_issues.py show <number> --owner <owner> --repo <repo>
```

If a request is under-specified (look for `*(not provided)*` fields), ask for
the missing detail with a comment and leave the issue open rather than guessing:

```sh
uv run scripts/gitea_issues.py comment <number> --owner <owner> \
    --repo <repo> --body "Which existing room should this draw its docs from?"
```

## Closing a request

Carry out the operation (see [Room creation](room_creation.md) /
[Room access](room_access.md)), then close the issue. A plain `close` records a
comment without a decision label:

```sh
uv run scripts/gitea_issues.py close <number> --owner <owner> \
    --repo <repo> --body "Done — room 'marketing' is live."
```

For room-access requests, prefer the `approve` / `deny` subcommands, which also
apply the `approved` / `denied` decision label — see
[Room access](room_access.md).

Only close once the operation is actually complete. Never close a request you
could not fulfil — comment what is blocking it and leave it open.
