---
name: soliplex-concierge-admin
description: |
    Act on Soliplex room requests filed as Gitea issues by the concierge room:
    list and read the open request issues, perform the requested operation
    (create a new room, or grant access to a private one), then comment the
    outcome and close the issue. Use when an administrator wants to triage and
    resolve pending Soliplex room requests in an external coding agent.
license: MIT
metadata:
  version: "0.4.0"
---

# Soliplex concierge admin

You run in an external coding agent (not inside the Soliplex server) to resolve
the room-request issues that the `soliplex-concierge-room` skill files on a Gitea
repository. For each request you read the issue, carry out the operation, record
what you did as a comment, and close the issue.

Use these sibling skills to do the actual provisioning:

- **`soliplex-template`** — generate/configure the target stack, create rooms,
  and create/wire RAG databases.
- **`soliplex-docs`** — reference for how rooms, access, skills, and RAG are
  configured.

## Configuration

`scripts/gitea_issues.py` talks to the Gitea REST API directly. Provide the
connection via flags or the environment:

- `--host` / `GITEA_HOST` — the Gitea base URL.
- `--token` / `GITEA_ACCESS_TOKEN` — an access token that can comment on and
  close issues (this admin token is usually more privileged than the room's
  issue-filing token).
- `--owner` / `--repo` — the tracking repository the concierge files against.

The script needs `httpx` (`pip install httpx`, or install the
`soliplex-concierge` package, which depends on it).

Run `init` once per tracking repository to create the request labels
(`new-room`, `room-access`, `approved`, `denied`) the workflow below relies on
(idempotent — re-running reports each label as `created` or `exists`):

```sh
python scripts/gitea_issues.py init --owner <owner> --repo <repo>
```

## Workflow

1. **Find pending requests.** List the open issues:

   ```sh
   python scripts/gitea_issues.py list --owner <owner> --repo <repo>
   ```

2. **Read a request.** Print its title, metadata, and body:

   ```sh
   python scripts/gitea_issues.py show <number> --owner <owner> --repo <repo>
   ```

   The body follows one of two templates the room skill fills in:
   - **New room request** — name, purpose, knowledge sources, access, and any
     requested tools/skills.
   - **Room access request** — the existing room and the justification.

3. **Perform the operation.** This is delegated to `soliplex-template` /
   `soliplex-docs`; this skill does not provision directly. Decide by request
   type:
   - **New room** — use `soliplex-template` to create the room and, if knowledge
     sources were given, create/wire a RAG database for it; follow
     `soliplex-docs` for room and skill configuration. Apply the requested
     access and tools/skills.
   - **Access to a private room** — grant the requesting user access via the
     deployment's access mechanism (e.g. OIDC group membership or the room's
     access configuration), per `soliplex-docs`.

   If a request is unclear or under-specified (look for `*(not provided)*`
   fields), comment asking for the missing detail instead of guessing, and
   leave the issue open.

4. **Record the outcome.** Comment what you did (room id created, access
   granted, follow-up needed):

   ```sh
   python scripts/gitea_issues.py comment <number> --owner <owner> \
       --repo <repo> --body "Created room 'marketing' and granted access."
   ```

5. **Resolve the issue.** Close it, optionally with a final comment in one step:

   ```sh
   python scripts/gitea_issues.py close <number> --owner <owner> \
       --repo <repo> --body "Done — room is live."
   ```

   Only close once the operation is actually complete. Never close a request you
   could not fulfil; comment what is blocking it and leave it open.

## Resolving room access requests

Access requests (filed with the `room-access` label, titled
`Room access request: <room>`) are resolved with the dedicated subcommands
below, which record the decision as both a label and a closing comment.

1. **Find the open access requests.** Filter by the type label:

   ```sh
   python scripts/gitea_issues.py search --type room-access \
       --owner <owner> --repo <repo>
   ```

   Read a request with `show <number>` and note the room id and the requesting
   user from the body (`Requested by:` / `## Room`).

2. **Approve.** Granting access mutates the running stack's authorization
   database, so run the `soliplex-cli room-authz` commands against the stack
   the same way `soliplex-template` does (a one-off `backend` container via
   `docker compose run --rm`, from the stack directory). `add-acl-entry`
   requires an existing `RoomPolicy`, so make the room private first if it is
   not already:

   ```sh
   docker compose run --rm backend \
     /app/.venv/bin/soliplex-cli room-authz make-private  /environment <room_id>
   docker compose run --rm backend \
     /app/.venv/bin/soliplex-cli room-authz add-acl-entry \
       --allow --email <user> /environment <room_id>
   ```

   (`/environment` is the in-container installation path; use
   `--preferred-username <name>` when the request gives a username rather than
   an email.) **Show the admin these grant commands and run them only after
   they explicitly confirm.** Only once the grant has actually succeeded,
   record the outcome and close the issue:

   ```sh
   python scripts/gitea_issues.py approve <number> --owner <owner> \
       --repo <repo> \
       --body "Granted <user> access to '<room>' via room-authz add-acl-entry."
   ```

3. **Deny.** No stack change is needed — just record the decision and close:

   ```sh
   python scripts/gitea_issues.py deny <number> --owner <owner> \
       --repo <repo> --body "<reason for denial>"
   ```

As above, never close a request you could not fulfil; if the grant fails or the
request is unclear, comment what is blocking it and leave the issue open.
