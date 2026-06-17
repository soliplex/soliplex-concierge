---
name: soliplex-concierge-installer
description: |
    Wire the `soliplex-concierge` extension into a target Soliplex stack by
    running this skill's bundled `scripts/install_concierge.py` (via `uv run`, no install
    needed). It creates the `about-<project>` room (hosting the
    `create_gitea_issue` tool and the `soliplex-concierge-room` skill) and
    merges the required `installation.yaml` entries. Use when a user wants to
    add room-request / Gitea-issue filing to an existing
    soliplex-template-generated stack.
license: MIT
metadata:
  version: "0.6.0"
---

# Soliplex concierge installer

You run in an external coding agent to add the **soliplex-concierge** extension
to a Soliplex deployment. The work is done by this skill's bundled
`scripts/install_concierge.py`, run with `uv run` (a thin shim that provisions
`soliplex-concierge` from its PEP 723 header — no `pip install` needed — then
delegates to the `soliplex_concierge.installer` module). The
room template it installs ships beside the script under `assets/`; your job is
to confirm the prerequisites, run it correctly, and verify the result.

This skill is meant to be used alongside two sibling skills:

- **`soliplex-template`** — generate or inspect the target Docker Compose stack.
  If the user does not yet have a stack, use it to create one first.
- **`soliplex-docs`** — reference for Soliplex configuration (installation.yaml,
  rooms, skills, RAG, secrets) when you need to explain or troubleshoot wiring.

## Prerequisites

1. `uv` on the PATH (used to run the bundled script with its dependency).
2. A `soliplex-template`-generated Docker Compose stack (the script edits
   `backend/pyproject.toml`, `backend/Dockerfile`, `backend/environment/`, and
   `.env`). If there is no stack, generate one with the `soliplex-template` skill.
3. A Gitea repository to file room requests against, and an access token whose
   account has **Write** access to it (not just Read): Gitea lets a Read-only
   account open issues but silently drops their labels, so requests would be
   filed untagged (see issue #59).
4. Network access to GitHub: two filesystem skills are downloaded from their
   published releases by default — `soliplex-concierge-room` (from
   `soliplex/soliplex-concierge`, the `room-skill-latest` pointer) and
   `soliplex-docs` (from `soliplex/soliplex`, the `docs-latest` pointer). Pin a
   build with `--room-skill-version` / `--docs-skill-version`, or install
   offline from a local copy with `--room-skill-dir` / `--docs-skill-dir` (see
   the flags below). Set `GITHUB_TOKEN` / `GH_TOKEN` to raise the GitHub rate
   limit if needed.

## Steps

Run the bundled script from this skill's directory with `uv run`.

1. **Dry-run first** to preview every change without writing:

   ```sh
   uv run scripts/install_concierge.py --stack-dir /path/to/stack \
       --owner <gitea-owner> --repo <gitea-repo> --dry-run
   ```

2. **Ask the operator** whether the Gitea host's TLS certificate chains to an
   enterprise / internal CA (i.e. one not in certifi's bundle). If so, pass
   `--with-truststore` so the `create_gitea_issue` tool verifies against the
   host OS trust store instead of certifi.

3. Apply for real once the dry-run looks right:

   ```sh
   uv run scripts/install_concierge.py --stack-dir /path/to/stack \
       --owner <gitea-owner> --repo <gitea-repo> --version <X.Y>
   ```

### Useful flags

- `--stack-dir` — stack root (default: current directory).
- `--owner` / `--repo` — Gitea owner and repository for filed issues.
- `--gitea-host` / `--gitea-token` — fill real `.env` values instead of the
  `GITEA_HOST` / `GITEA_ACCESS_TOKEN` placeholders.
- `--no-local-gitea` — skip local-Gitea auto-detection (see below) and keep the
  external-Gitea defaults (placeholder `.env` values and room `owner`/`repo`).
- `--version <X.Y>` — pin the `soliplex-concierge` dependency added to the
  stack. Omitting it leaves the dependency unpinned and prints a version-skew
  warning; `--version latest` opts into the newest release without the warning.
- `--with-truststore` — install the `soliplex-concierge[truststore]` extra so
  the tool (and the copied `scripts/gitea_issues.py`) verifies TLS against the
  OS trust store (an enterprise/internal CA) rather than certifi's bundle. If
  the bare dependency is already present, the extra is not added on a re-run —
  edit it by hand in that case.
- `--room-skill-version <tag>` / `--docs-skill-version <tag>` — published tag to
  install for the room skill (default: `room-skill-latest`) / the docs skill
  (default: `docs-latest`); e.g. a rolling build or `v0.4` / `v0.69`.
- `--room-skill-dir <dir>` / `--docs-skill-dir <dir>` — install the
  `soliplex-concierge-room` / `soliplex-docs` skill from a local directory
  instead of downloading (offline / development).
- `--room-id` — room id to create (default: `about_<compose-project-name>`).
- `--force` — overwrite an existing room/skill; `--dry-run` — report only.

## What it changes (seven idempotent edits)

1. adds `soliplex-concierge` to `backend/pyproject.toml` dependencies,
2. adds it to the `backend/Dockerfile` `uv add` block (the generated Dockerfile
   does `uv init --bare` and ignores the pyproject deps, so both are needed),
3. merges the `installation.yaml` entries it needs (`meta.tool_configs`,
   `environment`, `secrets`). The two `skill_configs` entries
   (`soliplex-concierge-room`, `soliplex-docs`) are added **only if the stack
   has an explicit filesystem `skill_configs` whitelist** — on a typical stack
   `skill_configs` is permissive, so the skills (installed under `./skills`) are
   auto-discovered and no entry is needed; if a whitelist *is* present, the
   install aborts unless you pass `--confirm-skill-whitelist` (so it never
   silently narrows your curated whitelist),
4. copies the `about_soliplex` room template into `rooms/<room-id>/` (renaming
   the directory and `id:`) and wires its `room_paths` entry,
5. downloads + copies the `soliplex-concierge-room` and `soliplex-docs`
   filesystem skills into `skills/`,
6. adds `GITEA_HOST` / `GITEA_ACCESS_TOKEN` placeholders to `.env` (skipped for a
   local-Gitea stack — see below), and
7. writes the admin `gitea_issues.py` request-triage CLI into `scripts/`
   (a thin shim over `soliplex_concierge.gitea_admin`; run later with
   `uv run scripts/gitea_issues.py …`). With `--with-truststore`, its PEP 723
   header gets the `[truststore]` extra too.

The about-room answers Soliplex questions from the bundled `soliplex-docs`
skill, so no RAG database is required. To also answer from a RAG database,
uncomment the `skill_configs` block in the room's `room_config.yaml` (it points
at the `soliplex-template` skill for generating one).

Re-running is safe: each edit is skipped if already present (use `--force` to
overwrite the copied rooms / skills / `scripts/gitea_issues.py`).

## Local Gitea service

When the stack was generated with `include_gitea`, its `docker-compose.yml`
defines a `gitea` service (backed by Postgres) and ships `scripts/init_gitea.py`.
The installer detects this and, rather than emitting placeholders, wires the
room's `create_gitea_issue` tool to `soliplex-admin/soliplex-requests` (the
account + repo that shim creates) and leaves `.env` for `init_gitea.py` to
populate. Order: run the installer → `docker compose up -d` →
`uv run scripts/init_gitea.py` → `docker compose up -d backend`. Override with
`--owner`/`--repo`, or opt out with `--no-local-gitea`.

## Verify

- The script prints a per-target summary (`added` / `unchanged`); confirm the
  room, both skills, and `installation.yaml` entries are accounted for.
- Confirm `backend/environment/skills/soliplex-concierge-room/SKILL.md` and
  `backend/environment/skills/soliplex-docs/SKILL.md` exist, and
  `installation.yaml` lists `skill_name: "soliplex-concierge-room"` and
  `skill_name: "soliplex-docs"`.
- Set real `GITEA_HOST` / `GITEA_ACCESS_TOKEN` in `.env`, then
  `docker compose build backend && docker compose up -d`.
- Confirm `scripts/gitea_issues.py` exists and runs:
  `uv run scripts/gitea_issues.py --help`.

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
release is cut. **Rolling** builds (`installer-skill-YYYY.MM.DD-<sha>`) are
continuous per-build snapshots, tagged with the build date and short commit
hash; the `installer-skill-latest` pointer always tracks the newest one, so the
default `latest` target for `diff`/`upgrade` means "the current tip of the
rolling line." `list` shows both newest first (marking the installed copy and
the `latest` pointer); narrow it with `list --kind release` or
`list --kind rolling`. To stay on stable milestones rather than the rolling
tip, pass an explicit `v…` `TAG` to `diff`/`upgrade`.

Set `GITHUB_TOKEN`/`GH_TOKEN` to raise the GitHub API rate limit. The helper
needs network access to PyPI (to provision `soliplex-skills` on first run) and
to `api.github.com`/`github.com`.
