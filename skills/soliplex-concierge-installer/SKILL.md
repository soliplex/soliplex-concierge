---
name: soliplex-concierge-installer
description: |
    Wire the `soliplex-concierge` extension into a target Soliplex stack:
    install the package and run the `soliplex-concierge-apply` console script,
    which creates the `about-<project>` room (hosting the `create_gitea_issue`
    tool and the `soliplex-concierge-room` skill) and merges the required
    `installation.yaml` entries. Use when a user wants to add room-request /
    Gitea-issue filing to an existing soliplex-template-generated stack.
license: MIT
metadata:
  version: "0.3.0"
---

# Soliplex concierge installer

You run in an external coding agent to add the **soliplex-concierge** extension
to a Soliplex deployment. The work is done by the `soliplex-concierge-apply`
console script (shipped by the `soliplex-concierge` PyPI package); your job is to
confirm the prerequisites, run it correctly, and verify the result.

This skill is meant to be used alongside two sibling skills:

- **`soliplex-template`** — generate or inspect the target Docker Compose stack.
  If the user does not yet have a stack, use it to create one first.
- **`soliplex-docs`** — reference for Soliplex configuration (installation.yaml,
  rooms, skills, RAG, secrets) when you need to explain or troubleshoot wiring.

## Prerequisites

1. A `soliplex-template`-generated Docker Compose stack (the script edits
   `backend/pyproject.toml`, `backend/Dockerfile`, `backend/environment/`, and
   `.env`). If there is no stack, generate one with the `soliplex-template` skill.
2. A Gitea repository to file room requests against, and an access token with
   permission to create issues in it.

## Steps

1. Install the package with the `apply` extra (it adds `ruamel.yaml`, used only
   by the installer):

   ```sh
   pip install "soliplex-concierge[apply]"
   ```

2. **Dry-run first** to preview every change without writing:

   ```sh
   soliplex-concierge-apply --stack-dir /path/to/stack \
       --owner <gitea-owner> --repo <gitea-repo> --dry-run
   ```

3. Apply for real once the dry-run looks right:

   ```sh
   soliplex-concierge-apply --stack-dir /path/to/stack \
       --owner <gitea-owner> --repo <gitea-repo> --version <X.Y>
   ```

### Useful flags

- `--stack-dir` — stack root (default: current directory).
- `--owner` / `--repo` — Gitea owner and repository for filed issues.
- `--gitea-host` / `--gitea-token` — fill real `.env` values instead of the
  `GITEA_HOST` / `GITEA_ACCESS_TOKEN` placeholders.
- `--version <X.Y>` — pin the `soliplex-concierge` dependency. Omitting it
  installs unpinned and prints a version-skew warning; `--version latest` opts
  into the newest release without the warning.
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

- The script echoes the `soliplex-concierge` version it installed — check it
  matches what you intended (and any `--version` pin).
- Confirm `backend/environment/skills/soliplex-concierge-room/SKILL.md` exists
  and `installation.yaml` lists `skill_name: "soliplex-concierge-room"`.
- Set real `GITEA_HOST` / `GITEA_ACCESS_TOKEN` in `.env`, then
  `docker compose build backend && docker compose up -d`.
