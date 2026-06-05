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
Docker Compose stack, the `soliplex-concierge-apply` console script performs
all of the wiring below for you, idempotently:

```sh
pip install -e ".[apply]"        # the 'apply' extra adds ruamel.yaml
soliplex-concierge-apply --stack-dir /path/to/your/stack
```

It adds the dependency to `backend/pyproject.toml` *and* the `backend/Dockerfile`
`uv add` block (the generated Dockerfile ignores the pyproject deps, so both are
needed), merges the five `installation.yaml` entries, installs the room and the
filesystem skill, and appends the `.env` placeholders. The room is installed as
`about_<compose-project-name>` by default; override with `--room-id`.

Pass `--version <X.Y>` to pin the dependency (e.g. `--version 0.2`); omitting
it leaves the dependency unpinned and prints a warning (unpinned installs are
prone to version skew between the stack and your wiring), while
`--version latest` opts into the newest release without the warning. After it
finishes, the script echoes the `soliplex-concierge` version installed in the
environment it ran from, so you can spot a mismatch.

The room bundles a `haiku.rag.skills.rag` skill, which needs a RAG LanceDB to
exist. By default the script wires it to the stack's existing
`rag/db/*.lancedb` (preferring `haiku.rag`, the template ingester's default),
falling back to `haiku.rag` when none is present yet; override with
`--rag-stem <name>` to point at a specific database.

Other flags: `--owner` / `--repo` / `--gitea-host` / `--gitea-token` (fill the
Gitea values instead of placeholders), `--force` (overwrite an existing
room/skill), and `--dry-run` (report the changes without writing). Afterwards,
set real Gitea values and `docker compose build backend && docker compose up -d`.

### Manual wiring

The script above automates exactly these steps; do them by hand for a
non-generated installation.

1. Install this package into the environment that runs Soliplex (it depends
   on `soliplex`):

   ```sh
   pip install -e .
   ```

2. Merge the entries from
   [`example/installation-snippet.yaml`](example/installation-snippet.yaml)
   into your `installation.yaml`. They:

   - register the tool-config class via `meta.tool_configs` (Soliplex
     resolves the tool by its dotted `tool_name`; no core edit is needed),
   - add this package's `skills/soliplex-concierge-room` directory to
     `filesystem_skills_paths` and enable the `soliplex-concierge-room`
     filesystem skill, and
   - declare the `GITEA_HOST` environment variable and `GITEA_ACCESS_TOKEN`
     secret that the `create_gitea_issue` tool reads to call the Gitea REST
     API (no MCP server or external binary is required).

3. Copy [`example/rooms/about_soliplex/`](example/rooms/about_soliplex) into
   your installation's `rooms/` directory, editing the `owner` / `repo` on
   the `create_gitea_issue` tool to point at your tracking repository.

4. Set `GITEA_HOST` and `GITEA_ACCESS_TOKEN` (see the `.env` lines at the
   bottom of the installation snippet).

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
