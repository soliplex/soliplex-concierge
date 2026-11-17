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

`scripts/gitea_issues.py` is a [PEP 723](https://peps.python.org/pep-0723/)
script: run it with `uv run`, which provisions its `soliplex-concierge`
dependency (and `httpx2`) automatically from the inline metadata. It just needs
`uv` on the `PATH`. It is a thin shim over `soliplex_concierge.gitea_admin`,
where the logic lives. The `soliplex-concierge-installer` skill drops an
identical copy into the target stack at `<stack>/scripts/gitea_issues.py`, so
you can run the same CLI from the deployed stack directory.

If the Gitea host's TLS certificate chains to an enterprise / internal CA (one
not in certifi's bundle), **ask the admin**, then add `--with truststore` to
the `uv run` invocation so the script verifies against the host OS trust store
instead — e.g. `uv run --with truststore scripts/gitea_issues.py list …`. No
edit to the script is needed; the extra package activates the OS-trust-store
path automatically (see
[issue #46](https://github.com/soliplex/soliplex-concierge/issues/46)).

Run `init` once per tracking repository to create the request labels
(`new-room`, `room-access`, `approved`, `denied`) the workflow below relies on
(idempotent — re-running reports each label as `created` or `exists`). The
concierge creates the two type labels itself when it files, but nothing else
creates `approved` / `denied`: until `init` has run, `approve` and `deny` fail
with a "missing label" error. If they do, run `init` and retry:

```sh
uv run scripts/gitea_issues.py init --owner <owner> --repo <repo>
```

## Issues the room provisioner files

On a stack running a room provisioner (a `soliplex-template` stack's
`soliplex-provisioner` service), some `new-room` issues are filed by the
provisioner itself, not by the room skill: it creates the room on the spot for
a user it admits, files the issue as its record, then comments and closes it
once the room is live. Recognize one by the ``- **Room id:** `<id>` `` line
in its body (the room skill's template has none).

**Do not create the room such an issue names** — the provisioner already
did, or tried to. An open one needs a look instead:

- **with a provisioner comment** — the comment says what failed, or which
  steps remain for an administrator to finish; do those steps (with
  `soliplex-template`), then close the issue with what you did;
- **with no comment** — the provisioner stopped mid-run. Check whether the
  room exists in the stack before doing anything, and report what you find
  to the admin rather than guessing.

## Workflow

1. **Find pending requests.** List the open issues:

   ```sh
   uv run scripts/gitea_issues.py list --owner <owner> --repo <repo>
   ```

2. **Read a request.** Print its title, metadata, and body:

   ```sh
   uv run scripts/gitea_issues.py show <number> --owner <owner> --repo <repo>
   ```

   The body follows one of two templates the room skill fills in:
   - **New room request** — name, purpose, knowledge sources, access, and any
     requested tools/skills.
   - **Room access request** — the existing room and the justification.

3. **Perform the operation.** This is delegated to `soliplex-template` /
   `soliplex-docs`; this skill does not provision directly. Decide by request
   type:
   - **New room** — first check it is not one the provisioner filed (see
     [above](#issues-the-room-provisioner-files)). Otherwise, use
     `soliplex-template` to create the room and, if knowledge sources were
     given, create/wire a RAG database for it; follow `soliplex-docs` for room
     and skill configuration. Apply the requested access and tools/skills.
   - **Access to a private room** — follow
     [Resolving room access requests](#resolving-room-access-requests) below:
     identify the requester from the attached user profile, grant with
     `soliplex-cli room-authz`, and record the decision with `approve` /
     `deny`.

   If a request is unclear or under-specified (look for `*(not provided)*`
   fields), comment asking for the missing detail instead of guessing, and
   leave the issue open.

4. **Record the outcome.** Comment what you did (room id created, access
   granted, follow-up needed):

   ```sh
   uv run scripts/gitea_issues.py comment <number> --owner <owner> \
       --repo <repo> --body "Created room 'marketing' and granted access."
   ```

5. **Resolve the issue.** Close it, optionally with a final comment in one step:

   ```sh
   uv run scripts/gitea_issues.py close <number> --owner <owner> \
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
   uv run scripts/gitea_issues.py search --type room-access \
       --owner <owner> --repo <repo>
   ```

   Read a request with `show <number>` and note the room from the body
   (`## Room`) and the justification.

   **Take the requester's identity from the attached user profile, never from
   the body.** The body's `Requested by:` line is written by the room's agent
   and may be wrong or `*(not provided)*`, and `show`'s `by:` is the Gitea
   account that filed the issue, not the requester. When it filed the issue,
   `create_gitea_issue` attached the signed-in user's OIDC claims as a YAML
   file (`user-profile.yaml` by default) — typically their `email` and
   `preferred_username`, the discriminators you grant against. `show` does
   not print attachments or comments; fetch them from the Gitea API:

   ```sh
   # the attachment: download its 'browser_download_url'
   curl -s -H "Authorization: token $GITEA_ACCESS_TOKEN" \
     "$GITEA_HOST/api/v1/repos/<owner>/<repo>/issues/<number>/assets"
   # if Gitea refused the attachment, the profile is a YAML block in a comment
   curl -s -H "Authorization: token $GITEA_ACCESS_TOKEN" \
     "$GITEA_HOST/api/v1/repos/<owner>/<repo>/issues/<number>/comments"
   ```

   An installation may set `exclude_claims` to keep some claims (e.g.
   `email`) out of the profile; use the other discriminator then. If neither
   a profile nor a usable claim is there, comment asking the requester to
   confirm their identity and leave the issue open — do not grant access
   based on the body alone.

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
       --allow --permission read --email <user> /environment <room_id>
   ```

   (`/environment` is the in-container installation path; use
   `--preferred-username <name>` when the profile gives a username rather than
   an email. `--permission read` is what entering the room needs: a `write`- or
   `admin`-only entry does not admit the user.) **Show the admin these grant
   commands and run them only after they explicitly confirm.** Only once the
   grant has actually succeeded, record the outcome and close the issue:

   ```sh
   uv run scripts/gitea_issues.py approve <number> --owner <owner> \
       --repo <repo> \
       --body \
       "Granted <user> read access to '<room>' via room-authz add-acl-entry."
   ```

3. **Deny.** No stack change is needed — just record the decision and close:

   ```sh
   uv run scripts/gitea_issues.py deny <number> --owner <owner> \
       --repo <repo> --body "<reason for denial>"
   ```

As above, never close a request you could not fulfil; if the grant fails or the
request is unclear, comment what is blocking it and leave the issue open.

## Managing this skill's version

`scripts/skill_versions.py` lists, diffs, and upgrades published builds of this
skill against its GitHub releases. It is a small
[PEP 723](https://peps.python.org/pep-0723/) helper backed by the shared
[`soliplex-skills`](https://soliplex.github.io/soliplex-skills/) library, so run
it with `uv` (the first run fetches that library):

```bash
uv run scripts/skill_versions.py list              # published versions, newest first
uv run scripts/skill_versions.py diff [TAG]         # installed vs a published build (default: latest)
uv run scripts/skill_versions.py upgrade [TAG]      # install a published build in place (default: latest)
```

Two kinds of versions are published. **Release** builds are snapshots attached
to tagged software releases (`v…`) — stable milestones that only change when a
release is cut. **Rolling** builds (`admin-skill-YYYY.MM.DD-<sha>`) are
continuous per-build snapshots, tagged with the build date and short commit
hash; the `admin-skill-latest` pointer always tracks the newest one, so the
default `latest` target for `diff`/`upgrade` means "the current tip of the
rolling line." `list` shows both newest first (marking the installed copy and
the `latest` pointer); narrow it with `list --kind release` or
`list --kind rolling`. To stay on stable milestones rather than the rolling
tip, pass an explicit `v…` `TAG` to `diff`/`upgrade`.

Set `GITHUB_TOKEN`/`GH_TOKEN` to raise the GitHub API rate limit. The helper
needs network access to PyPI (to provision `soliplex-skills` on first run) and
to `api.github.com`/`github.com`.
