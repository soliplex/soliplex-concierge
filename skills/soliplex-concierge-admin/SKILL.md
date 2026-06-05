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
  version: "0.3.0"
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
