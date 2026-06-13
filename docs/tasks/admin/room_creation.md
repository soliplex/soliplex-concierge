# Resolving a new-room request

A new-room request (the `new-room` label) asks for a room that does not yet
exist. Resolving it is two jobs: read the spec the requester provided, then
build the room with the [`soliplex-template`](https://github.com/soliplex/soliplex-template)
skill. The `soliplex-concierge-admin` skill handles the first job and records
the outcome; `soliplex-template` does the provisioning.

## 1. Read the spec

Find the open new-room requests and read one:

```sh
uv run scripts/gitea_issues.py search --type new-room \
    --owner <owner> --repo <repo>
uv run scripts/gitea_issues.py show <number> --owner <owner> --repo <repo>
```

The body follows the room-skill's new-room template, which captures:

- **Name and purpose** — what the room is for.
- **Knowledge sources** — the documents or data it should draw from (this drives
  whether a RAG database is needed).
- **Access** — who should be able to use it (public, or a specific set of users).
- **Tools / skills** — any capabilities the room should provide.

Any field the requester left blank shows as `*(not provided)*`; comment on the
issue to fill the gap rather than guessing (see
[Administering requests](index.md)).

## 2. Create the room

Use the `soliplex-template` skill against the target stack to create and
configure the room. Follow `soliplex-docs` for the configuration details:

- **Room configuration** — create the room and set its prompt, tools, and
  skills per the spec. See the
  [room configuration docs](https://soliplex.github.io/soliplex/config/rooms/).
- **RAG database** — if the request named knowledge sources, create and ingest a
  RAG database for them, then wire it into the room's `skill_configs`
  (`rag_lancedb_stem`). The `soliplex-template` skill can generate and ingest
  the database; see the
  [RAG DB docs](https://soliplex.github.io/soliplex/config/rag/).
- **Access** — if the room should be private, apply the requested access the
  same way as for an [access request](room_access.md) (`soliplex-cli room-authz`).

## 3. Record the outcome

Once the room is live, comment what you did and close the issue:

```sh
uv run scripts/gitea_issues.py close <number> --owner <owner> \
    --repo <repo> --body "Created room 'marketing' with a RAG DB over the 2024 decks; granted the marketing team access."
```

If the request could not be fulfilled, comment what is blocking it and leave the
issue open.
