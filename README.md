# `soliplex-concierge`: room access / creation support

This project provides a [Soliplex](https://github.com/soliplex/soliplex)
extension to support LLM-driven, on-demand creation of issues tracking
requested changes to a Soliplex installation's configuration:

- Access to a non-public room
- Creation of a new room

Components:

- The [Python library](src/soliplex_concierge) contains code to support
  both the issue creation tasks and the scripts which actually perform
  the requested updates.

- The [agent skills](skills/) contain
  [skill definitions](https://agentskills.io) which allow an agent to
  perform these tasks: `soliplex-concierge-installer` (wire the extension
  into a stack), `soliplex-concierge-room` (the in-room request formatter,
  copied into the stack), and `soliplex-concierge-admin` (act on and resolve
  filed requests).

## Install & wire up

### Generated stacks: one-shot apply

If your installation is a
[`soliplex-template`](https://github.com/soliplex/soliplex-template)-generated
Docker Compose stack, the `soliplex-concierge-installer` skill's bundled
`scripts/apply.py` performs all of the wiring below for you, idempotently. Run
it with `uv`, which provisions its one dependency (`ruamel.yaml`) from the
script's PEP 723 header — no install step needed:

```sh
uv run skills/soliplex-concierge-installer/scripts/apply.py \
    --stack-dir /path/to/your/stack --owner <gitea-owner> --repo <gitea-repo>
```

It adds the dependency to `backend/pyproject.toml` *and* the `backend/Dockerfile`
`uv add` block (the generated Dockerfile ignores the pyproject deps, so both are
needed), merges the five `installation.yaml` entries, installs the room and the
`soliplex-concierge-room` filesystem skill, and appends the `.env` placeholders.
The room is installed as `about_<compose-project-name>` by default; override
with `--room-id`.

Pass `--version <X.Y>` to pin the `soliplex-concierge` dependency added to the
stack (e.g. `--version 0.4`); omitting it leaves the dependency unpinned and
prints a warning (unpinned installs are prone to version skew between the stack
and your wiring), while `--version latest` opts into the newest release without
the warning.

The about-room answers Soliplex questions from the `soliplex-docs` skill (the
full Soliplex documentation), so no RAG database is required. To *also* answer
from a RAG database — e.g. this installation's own ingested content — uncomment
the `skill_configs` block in the installed room's `room_config.yaml`, pointing
`rag_lancedb_stem` at a database under `rag/db/` (the
[`soliplex-template`](https://github.com/soliplex/soliplex-template) skill can
generate and ingest one).

The room template is bundled beside the script (under `assets/`); the two
filesystem skills copied into the stack are **downloaded from their published
releases** by default, sha256 verified — `soliplex-concierge-room` (the
`room-skill-latest` pointer) and `soliplex-docs` (the `docs-latest` pointer,
from `soliplex/soliplex`). Pin a build with `--room-skill-version <tag>` /
`--docs-skill-version <tag>`, or install a local copy (offline / development)
with `--room-skill-dir <dir>` / `--docs-skill-dir <dir>`.

Other flags: `--gitea-host` / `--gitea-token` (fill the Gitea values instead of
placeholders), `--force` (overwrite an existing room/skill), and `--dry-run`
(report the changes without writing). Afterwards, set real Gitea values and
`docker compose build backend && docker compose up -d`.

### Manual wiring

The script above automates exactly these steps; do them by hand for a
non-generated installation.

1. Install this package into the environment that runs Soliplex (it depends
   on `soliplex`):

   ```sh
   pip install -e .
   ```

2. Merge the entries from
   [`installation-snippet.yaml`](skills/soliplex-concierge-installer/assets/installation-snippet.yaml)
   (bundled in the installer skill's `assets/`) into your `installation.yaml`.
   They:

   - register the tool-config class via `meta.tool_configs` (Soliplex
     resolves the tool by its dotted `tool_name`; no core edit is needed),
   - add this package's `skills/soliplex-concierge-room` directory to
     `filesystem_skills_paths` and enable the `soliplex-concierge-room`
     filesystem skill, and
   - declare the `GITEA_HOST` environment variable and `GITEA_ACCESS_TOKEN`
     secret that the `create_gitea_issue` tool reads to call the Gitea REST
     API (no MCP server or external binary is required).

3. Copy
   [`rooms/about_soliplex/`](skills/soliplex-concierge-installer/assets/rooms/about_soliplex)
   into your installation's `rooms/` directory, editing the `owner` / `repo` on
   the `create_gitea_issue` tool to point at your tracking repository.

4. Set `GITEA_HOST` and `GITEA_ACCESS_TOKEN` (see the `.env` lines at the
   bottom of the installation snippet). The token's account must have **Write**
   access to the tracking repo, not just Read — Gitea lets a Read-only account
   open issues but silently drops their labels, so filed requests would arrive
   untagged (see issue #59).

## Skill releases

Each skill under `skills/` is published from CI
([`build-skills.yaml`](.github/workflows/build-skills.yaml)) as a standalone
GitHub release artifact, following the pattern `soliplex` uses for its skills:

- **Rolling builds** — every change to `skills/**` on `main` publishes an
  immutable `<prefix>-YYYY.MM.DD-<sha>` prerelease (prefixes: `installer-skill`,
  `room-skill`, `admin-skill`) and updates a `<prefix>-latest` pointer; the ten
  newest rolling builds per skill are kept.
- **Tagged releases** — publishing a software release (`v*`) attaches all three
  skill tarballs to that release, pinned to the version.

Each published skill bundles `scripts/skill_versions.py`, so an installed copy
can `list`, `diff`, and `upgrade` itself against the published builds, e.g.
`python scripts/skill_versions.py list`.

## Status

The issue-filing concierge (the `about_soliplex` room, its `create_gitea_issue`
tool, and the `soliplex-concierge-room` skill) is implemented, as is the
`soliplex-concierge-admin` skill, whose scripts read, comment on, and close
(resolve) the filed request issues. The actual provisioning those requests ask
for — creating rooms and granting access — is driven through the
`soliplex-template` skill (see the admin skill's workflow); turnkey automation of
that step is a planned follow-up.
