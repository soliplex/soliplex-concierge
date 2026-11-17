# Setting up the concierge

Adding the concierge to an installation means creating the about-room,
wiring in the `soliplex-concierge-room` and `soliplex-docs` skills,
registering the `create_gitea_issue`, `request_room` and `check_room_request`
tools, and declaring the Gitea connection. This page covers:

- [installing it with the `soliplex-concierge-installer` skill](#the-installer-skill-recommended),
  which does all of this for you on a
  [`soliplex-template`](https://github.com/soliplex/soliplex-template)-generated
  stack
- [customized installations](#customized-installations): what to watch for
  on a stack `soliplex-template` did not generate, and the manual steps
- [creating the request labels](#create-the-request-labels), once per
  tracking repository, however you installed
- [configuring the about-room](#configuring-the-about-room), however it was
  installed: what it needs to create rooms on request, bringing an older
  about-room up to date, and what it answers questions from

Either way you install it, you will need a Gitea repository to file requests
against, and an access token whose account has **Write** access to it — not
just Read. Gitea lets a Read-only account open issues but silently drops their
labels, so requests would be filed untagged and slip past triage
([issue #59](https://github.com/soliplex/soliplex-concierge/issues/59)).
A fine-grained token additionally needs the `write:issue` and `read:repository`
scopes.

## The installer skill (recommended)

Run the `soliplex-concierge-installer` skill from an external coding agent
(e.g. Claude Code). It runs its bundled `scripts/install_concierge.py` with
`uv run` — a thin shim that provisions `soliplex-concierge` from its
[PEP 723](https://peps.python.org/pep-0723/) header (so no install step is
needed) and delegates to the `soliplex_concierge.installer` module — applying
every change idempotently.

Preview first with `--dry-run`, then apply:

```sh
# dry run — report changes without writing
uv run scripts/install_concierge.py --stack-dir /path/to/stack \
    --owner <gitea-owner> --repo <gitea-repo> --dry-run

# apply, pinning the dependency version
uv run scripts/install_concierge.py --stack-dir /path/to/stack \
    --owner <gitea-owner> --repo <gitea-repo> --version <X.Y>
```

### What it changes

It applies these idempotent edits to the stack:

- Adds `soliplex-concierge` to `backend/pyproject.toml`, **and** to the list
  of packages the backend image installs: `backend/requirements.txt` in a
  stack built on a soliplex release image, or the `backend/Dockerfile`
  `uv add` block in an older stack (whose Dockerfile ignores the
  `pyproject.toml` deps).
- Merges its entries into `backend/environment/installation.yaml`:
  `meta.tool_configs` (the three tool-config classes), `environment`,
  `secrets`, and `room_paths`. On a stack with an explicit filesystem
  `skill_configs` allow-list, it also adds the two skills there, but only
  with `--confirm-skill-whitelist`; otherwise it stops with an error.
- Copies the about-room into `backend/environment/rooms/<room-id>/`
  (`about_<compose-project-name>` by default; override with `--room-id`).
- Downloads and installs the `soliplex-concierge-room` and `soliplex-docs`
  filesystem skills into `backend/environment/skills/`, stripping their
  `scripts/skill_versions.py` helper (room users must not reach it).
- Appends the `GITEA_HOST` / `GITEA_ACCESS_TOKEN` placeholders to `.env` —
  unless the stack ships a **local Gitea service** (see below), in which case
  `.env` is left for that service's own `scripts/init_gitea.py` to populate.
- Writes the admin `scripts/gitea_issues.py` CLI (see
  [Admin](../admin/index.md)).

It does **not** create the request labels on the tracking repository; see
[Create the request labels](#create-the-request-labels).

### Useful flags

- `--version <X.Y>` — pin the dependency added to the stack; omitting it warns
  about version skew, `--version latest` opts into the newest release.
- `--room-id` — room id to create (default `about_<compose-project-name>`).
- `--gitea-host` / `--gitea-token` — fill real `.env` values instead
  of placeholders.
- `--no-local-gitea` — skip local-Gitea auto-detection (see below) and keep the
  external-Gitea defaults (placeholder `.env` values and room `owner`/`repo`).
- `--with-truststore` — install the `soliplex-concierge[truststore]` extra
  so the tool verifies TLS against the OS trust store rather than `certifi`'s
  bundle; use when the Gitea host's certificate chains to an enterprise /
  internal CA
  ([issue #46](https://github.com/soliplex/soliplex-concierge/issues/46)).
- `--room-skill-dir` / `--docs-skill-dir` (or `--room-skill-version` /
  `--docs-skill-version`) — install the skills from a local copy or a pinned
  tag instead of downloading the latest.
- `--force` — overwrite an existing room/skill. This replaces the whole room
  directory, so any edits to its `room_config.yaml` (the Gitea `owner` /
  `repo`, `exclude_claims`, a RAG `skill_configs` block, tool settings) and
  `prompt.txt` are lost: copy them aside first and re-apply them afterwards.

Afterwards, set the real Gitea values in `.env` and rebuild:
`docker compose build backend && docker compose up -d`. Confirm both skills are
present under `backend/environment/skills/` and that
`uv run scripts/gitea_issues.py --help` works, then
[create the request labels](#create-the-request-labels).

### Local Gitea service

When the target stack was generated by `soliplex-template` with
`include_gitea`, its `docker-compose.yml` defines a `gitea` service (backed by
Postgres) and the stack ships a `scripts/init_gitea.py` shim that provisions
it. The installer detects that service and, instead of emitting dead
placeholders:

- wires the room's `create_gitea_issue` tool to
  `soliplex-admin/soliplex-requests` — the service account and repository
  `init_gitea.py` creates, and
- leaves `.env` untouched, since `init_gitea.py` writes `GITEA_HOST` /
  `GITEA_ACCESS_TOKEN` itself.

The order is then: run the installer → `docker compose up -d` →
`uv run scripts/init_gitea.py` (mints the token, creates the repo, populates
`.env`) → `docker compose up -d backend` to pick up the new `.env` →
[create the request labels](#create-the-request-labels). Pass `--owner` /
`--repo` to override the defaults, or `--no-local-gitea` to opt out of
detection entirely (for an external Gitea).

## Customized installations

!!! warning "Prefer a `soliplex-template` stack"

    The installer only works on a stack `soliplex-template` generated: it
    refuses any directory lacking that stack's files (`docker-compose.yml`,
    `backend/environment/installation.yaml`, `backend/pyproject.toml`,
    `backend/Dockerfile`, `.env`). Everything else the concierge assumes —
    a room provisioner service, the signing secret, the `skills/` layout, a
    local Gitea — also comes from that template. If you can, generate the
    stack with `soliplex-template` and let the installer wire it; wiring by
    hand means owning every step the installer would have checked for you,
    and re-doing them yourself on each upgrade.

### Pitfalls

Check each of these when wiring the concierge into an installation
`soliplex-template` did not generate.

- **`skill_configs` turns discovery into an allow-list.** `skill_configs` in
  `installation.yaml` is an allow-list per skill `kind`. While it has no
  `kind: "filesystem"` entries, Soliplex loads every filesystem skill it
  discovers. As soon as it has *one*, it loads *only* the filesystem skills
  listed.

    If your installation has no filesystem `skill_configs` entries, do
    **not** add the snippet's two: doing so would silently disable every
    other filesystem skill. If it already has an allow-list, add both
    `soliplex-concierge-room` and `soliplex-docs` to it, or the about-room
    loses its skills.

- **There may be no room provisioner.** The about-room's `request_room` and
  `check_room_request` tools call `http://soliplex-provisioner:8090` by
  default, a service only a `soliplex-template` stack runs, and sign their
  requests with the installation's `URL_SAFE_TOKEN_SECRET`, which only such a
  stack declares for you. Without them, every new-room request makes one
  failed call (`ServiceUnavailable` or `SigningSecretMissing`), and the error
  tells the agent to file the request as an issue instead — so users still
  get an issue, just never a room on the spot.

    The simplest choice is to **leave the tools in place** and accept that
    fallback. To create rooms on request instead, run a provisioner of your
    own (see [Implementing a provisioner](../provisioners/index.md)), point
    each tool's `service_url` at it, and declare `URL_SAFE_TOKEN_SECRET`.
    Do **not** simply delete the two tools from `room_config.yaml`: the
    room's `prompt.txt` tells the agent to call `request_room` first, so you
    would have to rewrite its new-room instructions as well.

- **The package must be in the image, not just the project.** The backend
  imports the tools from `soliplex-concierge`, so it must be installed
  wherever the backend runs. In a container, that means whatever the image
  build installs from; an entry only in a `pyproject.toml` the Dockerfile
  ignores is not enough.
- **Copying the skills by hand keeps their version helper.** The published
  skill tarballs contain `scripts/skill_versions.py`, which rewrites files and
  calls out to GitHub and PyPI. The installer strips it from the copies it
  installs, so room users cannot reach it; when you unpack a tarball
  yourself, delete `scripts/skill_versions.py` from it.
- **The room id lives in two places.** The template's `room_config.yaml`
  says `id: "about_soliplex"`. If you copy it under another directory name,
  change `id` to match, and add the room's directory to `room_paths` if your
  installation does not already discover it.
- **Placeholders stay placeholders.** The template's tool entry files to
  `your-gitea-owner/soliplex-requests` until you edit `owner` / `repo`.
- **Nothing creates the decision labels.** See
  [Create the request labels](#create-the-request-labels).

### Manual wiring

With those in mind, do the installer's steps by hand.

1. **Install the package** into the environment that runs Soliplex (it depends
   on `soliplex`). In a containerized stack, add `soliplex-concierge` to
   whatever the backend image installs from — `backend/requirements.txt`, or
   the `uv add` block in an older stack's `backend/Dockerfile` — and rebuild
   the image. For an environment managed directly:

    ```sh
    # a uv-managed project
    uv add soliplex-concierge
    # an existing virtual environment
    uv pip install soliplex-concierge
    ```

    For an enterprise / internal CA, install `soliplex-concierge[truststore]`
    instead so the tool verifies TLS against the OS trust store
    ([issue #46](https://github.com/soliplex/soliplex-concierge/issues/46)).

2. **Merge the `installation.yaml` entries** from the bundled
   [`installation-snippet.yaml`](https://github.com/soliplex/soliplex-concierge/blob/main/skills/soliplex-concierge-installer/assets/installation-snippet.yaml).
   They register the tool-config classes via `meta.tool_configs` (Soliplex
   resolves each tool by its dotted `tool_name`; no core edit is needed), add
   the directories of the two filesystem skills the about-room uses
   (`soliplex-concierge-room` and `soliplex-docs`) to
   `filesystem_skills_paths`, and declare the `GITEA_HOST` environment
   variable and `GITEA_ACCESS_TOKEN` secret the tool reads to call the Gitea
   REST API (no MCP server or external binary is required).

    The snippet also lists both skills under `skill_configs`. Merge that part
    **only** if your installation already has filesystem `skill_configs`
    entries (see [Pitfalls](#pitfalls)).

3. **Install the skills.** Unpack the
   [`soliplex-concierge-room`](../../skills/soliplex-concierge-room.md) and
   [`soliplex-docs`](https://soliplex.github.io/soliplex/docs-skill/)
   release tarballs into the directories you listed in
   `filesystem_skills_paths`, then delete `scripts/skill_versions.py` from
   each.

4. **Copy the about-room** template from
   [`rooms/about_soliplex/`](https://github.com/soliplex/soliplex-concierge/tree/main/skills/soliplex-concierge-installer/assets/rooms/about_soliplex)
   into your installation's `rooms/` directory, editing the `owner` / `repo` on
   the `create_gitea_issue` tool to point at your tracking repository. (Either
   may be a literal value or a Soliplex interpolation string such as
   `env:GITEA_OWNER`.) Decide what to do about the provisioner tools (see
   [Pitfalls](#pitfalls)).

5. **Set the Gitea connection** — `GITEA_HOST` and the `GITEA_ACCESS_TOKEN`
   secret (see the `.env` lines at the bottom of the snippet), using a token
   with Write access as noted above.

6. **Create the request labels**, as below.

## Create the request labels

Run the admin CLI's `init` **once per tracking repository**, after the
repository exists and before anyone approves or denies a request:

```sh
uv run scripts/gitea_issues.py init --owner <owner> --repo <repo>
```

It creates the four request labels (`new-room`, `room-access`, `approved`,
`denied`), and is idempotent — re-running reports each as `created` or
`exists`. The about-room creates the two type labels itself when it first
files a request of that type, but **nothing else creates `approved` and
`denied`**: until `init` has run, `gitea_issues.py approve` and `deny` fail
with a "missing label" error. The connection comes from `--host` /
`GITEA_HOST` and `--token` / `GITEA_ACCESS_TOKEN`; see
[Administering requests](../admin/index.md#the-gitea_issuespy-cli).

## Configuring the about-room

The `request_room` and `check_room_request` tools (see
[New rooms on request](index.md#new-rooms-on-request)) need:

- the address of the stack's room provisioner service: each tool's
  `service_url`, which defaults to `http://soliplex-provisioner:8090`, a
  `soliplex-template` stack's service
- the installation's `URL_SAFE_TOKEN_SECRET` secret, to sign their requests
  (a `soliplex-template` stack declares it)

Whether a user may create rooms is the provisioner's call: it admits admins,
and users granted WRITE on the reserved room id `__provision__`. The
`soliplex-template` docs on
[the provisioner service](https://soliplex.github.io/soliplex-template/users/operations/room-provisioning/service/)
show how to write that policy.

An about-room installed before these tools existed lacks them: re-run the
installer with `--force` to replace the room (which also replaces any edits
made to it — see `--force` under [Useful flags](#useful-flags)), or add the
two tool entries from the bundled
[`room_config.yaml`](https://github.com/soliplex/soliplex-concierge/blob/main/skills/soliplex-concierge-installer/assets/rooms/about_soliplex/room_config.yaml)
and the new `prompt.txt` by hand.

The about-room answers questions from the bundled `soliplex-docs` skill, so no
RAG database is required. To *also* answer from this installation's own
ingested content, uncomment the `skill_configs` block in the room's
`room_config.yaml`, pointing `rag_lancedb_stem` at a database under `rag/db/`.
The [`soliplex-template`](https://github.com/soliplex/soliplex-template) skill
can generate and ingest one (see the
[RAG DB docs](https://soliplex.github.io/soliplex/config/rag/)).
