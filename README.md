# `soliplex-concierge`: room access / creation support

This project provides a [Soliplex](https://github.com/soliplex/soliplex)
extension for LLM-driven, on-demand room requests: users chat with a self-serve
"about" room to request **access to a non-public room** or the **creation of a
new room**, and the concierge files each request as a tracking issue on a Gitea
repository for an administrator to resolve.

📖 **Full documentation:** <https://soliplex.github.io/soliplex-concierge/>

## Components

- The [Python library](src/soliplex_concierge) backs both the in-room
  issue-filing tool and the admin scripts that resolve filed requests (see the
  [API reference](docs/reference/api.md)).
- Three [Agent Skills](https://agentskills.io) under [`skills/`](skills/):

  - `soliplex-concierge-installer` — wire the extension into a stack
  - `soliplex-concierge-room` — the in-room request formatter, installed into
    the about-room
  - `soliplex-concierge-admin` — triage and resolve the filed requests

  See [Skills](docs/skills/index.md) for where to find each one and how to
  install it.

## Quick start

Installing the concierge onto a
[`soliplex-template`](https://github.com/soliplex/soliplex-template)-generated
stack is a job for an AI coding agent (e.g. Claude Code) running the
**`soliplex-concierge-installer`** skill — you point it at your stack rather
than running the wiring script yourself:

1. **Install the skill into your coding agent** — download the latest
   [`soliplex-concierge-installer`](docs/skills/soliplex-concierge-installer.md)
   release and unpack it into the agent's skills directory.
2. **Ask the agent to wire up your stack**, giving it the stack path and your
   Gitea owner / repo. The skill runs its bundled `scripts/apply.py` to apply
   [what it changes](docs/tasks/rooms/setup.md#what-it-changes), idempotently.
3. **Bring up the stack** — set real Gitea values in `.env`, then
   `docker compose build backend && docker compose up -d`.

The Gitea token's account must have **Write** access to the tracking repo, not
just Read — Gitea lets a Read-only account open issues but silently drops their
labels, so requests would be filed untagged (see issue #59). A fine-grained
token additionally needs the `write:issue` and `read:repository` scopes.

See [Setup](docs/tasks/rooms/setup.md) for the
[full option list](docs/tasks/rooms/setup.md#useful-flags) (version pinning,
`--with-truststore` for an enterprise / internal CA, …), running `apply.py`
directly, and the [manual-wiring steps](docs/tasks/rooms/setup.md#manual-wiring)
for a non-generated installation.

Once the stack is up, users file requests through the about-room — and you
resolve them from your coding agent with the
[`soliplex-concierge-admin`](docs/skills/soliplex-concierge-admin.md) skill
(installed the same way as the installer). Its `gitea_issues.py` CLI lists and
filters open requests, follows up on them, and closes each with a comment and a
decision label. See [Admin](docs/tasks/admin/index.md) for the workflow.
