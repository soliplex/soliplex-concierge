---
name: soliplex-concierge-installer
description: |
    Wire the `soliplex-concierge` extension into a target Soliplex stack by
    running this skill's bundled `scripts/apply.py` (via `uv run`, no install
    needed). It creates the `about-<project>` room (hosting the
    `create_gitea_issue` tool and the `soliplex-concierge-room` skill) and
    merges the required `installation.yaml` entries. Use when a user wants to
    add room-request / Gitea-issue filing to an existing
    soliplex-template-generated stack.
license: MIT
metadata:
  version: "0.4.0"
---

# Soliplex concierge installer

You run in an external coding agent to add the **soliplex-concierge** extension
to a Soliplex deployment. The work is done by this skill's bundled
`scripts/apply.py`, run with `uv run` (which provisions its one dependency,
`ruamel.yaml`, from the script's PEP 723 header — no `pip install` needed). The
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
3. A Gitea repository to file room requests against, and an access token with
   permission to create issues in it.
4. The `soliplex-concierge-room` skill tree to install into the stack. When you
   run this script from a source checkout it is found automatically beside this
   skill (`skills/soliplex-concierge-room`); otherwise fetch that skill and pass
   its directory with `--room-skill-dir` (see the flag below).

## Steps

Run the bundled script from this skill's directory with `uv run`.

1. **Dry-run first** to preview every change without writing:

   ```sh
   uv run scripts/apply.py --stack-dir /path/to/stack \
       --owner <gitea-owner> --repo <gitea-repo> --dry-run
   ```

2. Apply for real once the dry-run looks right:

   ```sh
   uv run scripts/apply.py --stack-dir /path/to/stack \
       --owner <gitea-owner> --repo <gitea-repo> --version <X.Y>
   ```

### Useful flags

- `--stack-dir` — stack root (default: current directory).
- `--owner` / `--repo` — Gitea owner and repository for filed issues.
- `--gitea-host` / `--gitea-token` — fill real `.env` values instead of the
  `GITEA_HOST` / `GITEA_ACCESS_TOKEN` placeholders.
- `--version <X.Y>` — pin the `soliplex-concierge` dependency added to the
  stack. Omitting it leaves the dependency unpinned and prints a version-skew
  warning; `--version latest` opts into the newest release without the warning.
- `--room-skill-dir` — directory of the `soliplex-concierge-room` skill to
  install (default: the checkout sibling `skills/soliplex-concierge-room`;
  required when running from an unpacked release bundle).
- `--room-id` — room id to create (default: `about_<compose-project-name>`).
- `--rag-stem` — RAG LanceDB stem to wire into the room's `haiku.rag.skills.rag`
  skill (default: the stack's existing `rag/db/*.lancedb`, else `haiku.rag`).
- `--force` — overwrite an existing room/skill; `--dry-run` — report only.

## What it changes (six idempotent edits)

1. adds `soliplex-concierge` to `backend/pyproject.toml` dependencies,
2. adds it to the `backend/Dockerfile` `uv add` block (the generated Dockerfile
   does `uv init --bare` and ignores the pyproject deps, so both are needed),
3. merges five `installation.yaml` entries (`meta.tool_configs`, `environment`,
   `secrets`, `skill_configs`, `room_paths`),
4. copies the `about_soliplex` room template into `rooms/<room-id>/` (renaming
   the directory, `id:`, and the `room_paths` entry),
5. copies the `soliplex-concierge-room` filesystem skill into `skills/`, and
6. adds `GITEA_HOST` / `GITEA_ACCESS_TOKEN` placeholders to `.env`.

Re-running is safe: each edit is skipped if already present (use `--force` to
overwrite the copied room/skill).

## Verify

- The script prints a per-target summary (`added` / `unchanged`); confirm the
  room, skill, and `installation.yaml` entries are accounted for.
- Confirm `backend/environment/skills/soliplex-concierge-room/SKILL.md` exists
  and `installation.yaml` lists `skill_name: "soliplex-concierge-room"`.
- Set real `GITEA_HOST` / `GITEA_ACCESS_TOKEN` in `.env`, then
  `docker compose build backend && docker compose up -d`.
