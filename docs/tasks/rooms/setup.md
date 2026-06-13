# Setting up the concierge

Adding the concierge to an installation means creating the about-room, wiring in
the `soliplex-concierge-room` and `soliplex-docs` skills, registering the
`create_gitea_issue` tool, and declaring the Gitea connection. The
**`soliplex-concierge-installer`** skill does all of this for you on a
[`soliplex-template`](https://github.com/soliplex/soliplex-template)-generated
stack; the [manual steps](#manual-wiring) below are for any other installation.

Either way you will need a Gitea repository to file requests against, and an
access token whose account has **Write** access to it — not just Read. Gitea
lets a Read-only account open issues but silently drops their labels, so requests
would be filed untagged and slip past triage (issue #59). A fine-grained token
additionally needs the `write:issue` and `read:repository` scopes.

## The installer skill (recommended)

Run the `soliplex-concierge-installer` skill from an external coding agent (e.g.
Claude Code). It runs its bundled `scripts/apply.py` with `uv run` — which
provisions the script's one dependency (`ruamel.yaml`) from its
[PEP 723](https://peps.python.org/pep-0723/) header, so no install step is
needed — and applies every change idempotently.

Preview first with `--dry-run`, then apply:

```sh
# dry run — report changes without writing
uv run scripts/apply.py --stack-dir /path/to/stack \
    --owner <gitea-owner> --repo <gitea-repo> --dry-run

# apply, pinning the dependency version
uv run scripts/apply.py --stack-dir /path/to/stack \
    --owner <gitea-owner> --repo <gitea-repo> --version <X.Y>
```

It performs seven idempotent edits: it adds `soliplex-concierge` to
`backend/pyproject.toml` **and** the `backend/Dockerfile` `uv add` block (the
generated Dockerfile ignores the pyproject deps, so both are needed); merges six
`installation.yaml` entries (`meta.tool_configs`, `environment`, `secrets`, two
`skill_configs` — for `soliplex-concierge-room` and `soliplex-docs` — and
`room_paths`); copies the about-room template into `rooms/<room-id>/`; downloads
and installs both filesystem skills into `skills/`; appends the `GITEA_HOST` /
`GITEA_ACCESS_TOKEN` placeholders to `.env`; and writes the admin request-triage
CLI to `scripts/gitea_issues.py` (see [Admin](../admin/index.md)).

Useful flags:

- `--version <X.Y>` — pin the dependency added to the stack; omitting it warns
  about version skew, `--version latest` opts into the newest release.
- `--room-id` — room id to create (default `about_<compose-project-name>`).
- `--gitea-host` / `--gitea-token` — fill real `.env` values instead of placeholders.
- `--with-truststore` — install the `soliplex-concierge[truststore]` extra so the
  tool verifies TLS against the OS trust store rather than certifi's bundle; use
  when the Gitea host's certificate chains to an enterprise / internal CA (issue #46).
- `--room-skill-dir` / `--docs-skill-dir` (or `--room-skill-version` /
  `--docs-skill-version`) — install the skills from a local copy or a pinned tag
  instead of downloading the latest.
- `--force` — overwrite an existing room/skill.

Afterwards, set the real Gitea values in `.env` and rebuild:
`docker compose build backend && docker compose up -d`. Confirm both skills are
present under `backend/environment/skills/` and that
`uv run scripts/gitea_issues.py --help` works.

## Manual wiring

For a non-generated installation, do the same steps by hand.

1. **Install the package** into the environment that runs Soliplex (it depends
   on `soliplex`):

   ```sh
   pip install -e .
   ```

   For an enterprise / internal CA, install `soliplex-concierge[truststore]`
   instead so the tool verifies TLS against the OS trust store (issue #46).

2. **Merge the `installation.yaml` entries** from the bundled
   [`installation-snippet.yaml`](https://github.com/soliplex/soliplex-concierge/blob/main/skills/soliplex-concierge-installer/assets/installation-snippet.yaml).
   They:
   - register the tool-config class via `meta.tool_configs` (Soliplex resolves
     the tool by its dotted `tool_name`; no core edit is needed),
   - add the directories of the two filesystem skills the about-room uses
     (`soliplex-concierge-room` and `soliplex-docs`) to `filesystem_skills_paths`,
     and enable both under `skill_configs`, and
   - declare the `GITEA_HOST` environment variable and `GITEA_ACCESS_TOKEN`
     secret the tool reads to call the Gitea REST API (no MCP server or external
     binary is required).

3. **Copy the about-room** template from
   [`rooms/about_soliplex/`](https://github.com/soliplex/soliplex-concierge/tree/main/skills/soliplex-concierge-installer/assets/rooms/about_soliplex)
   into your installation's `rooms/` directory, editing the `owner` / `repo` on
   the `create_gitea_issue` tool to point at your tracking repository. (Either
   may be a literal value or a Soliplex interpolation string such as
   `env:GITEA_OWNER`.)

4. **Set the Gitea connection** — `GITEA_HOST` and the `GITEA_ACCESS_TOKEN`
   secret (see the `.env` lines at the bottom of the snippet), using a token with
   Write access as noted above.

The about-room answers questions from the bundled `soliplex-docs` skill, so no
RAG database is required. To *also* answer from this installation's own ingested
content, uncomment the `skill_configs` block in the room's `room_config.yaml`,
pointing `rag_lancedb_stem` at a database under `rag/db/` — the
[`soliplex-template`](https://github.com/soliplex/soliplex-template) skill can
generate and ingest one (see the
[RAG DB docs](https://soliplex.github.io/soliplex/config/rag/)).
