# Python API reference

The public symbols of the `soliplex_concierge` package. Most users never import
these directly — the room tool is wired in by configuration (see
[Setup](../tasks/rooms/setup.md)) and the admin functions are driven through the
`gitea_issues.py` CLI (see [Administering requests](../tasks/admin/index.md)).
The reference is for extension authors and for scripting against the admin
helpers.

## `soliplex_concierge.config`

Tool-configuration types registered with a Soliplex installation under
`meta.tool_configs`.

**Constants**

- `CGI_TOOL_NAME: str` — the dotted tool name,
  `"soliplex_concierge.tools.gitea.create_gitea_issue"`.
- `CGI_TOOL_KIND: str` — the tool kind (the final segment of `CGI_TOOL_NAME`).
- `DEFAULT_PROFILE_ATTACHMENT_NAME: str` — default filename for the attached
  user profile (`"user-profile.yaml"`).
- `RR_TOOL_NAME` / `RR_TOOL_KIND`, `CRR_TOOL_NAME` / `CRR_TOOL_KIND` — the
  dotted names and kinds of the `request_room` and `check_room_request` tools.
- `DEFAULT_PROVISIONER_URL: str` — the room provisioner service of a
  `soliplex-template` stack (`"http://soliplex-provisioner:8090"`).
- `DEFAULT_REQUEST_ROOM_TIMEOUT: float` — how long `request_room` waits for
  the service's answer (`120.0` seconds).

**`class CreateGiteaIssueToolConfig(soliplex.config.tools.ToolConfig)`**

Dataclass describing the `create_gitea_issue` tool for a room. Configured keys:

- `owner`, `repo` — the tracking repository (required).
- `host` — Gitea base URL (default interpolation `"env:GITEA_HOST"`).
- `token` — access token (default interpolation `"secret:GITEA_ACCESS_TOKEN"`).
- `profile_attachment_name` — attachment filename (default
  `DEFAULT_PROFILE_ATTACHMENT_NAME`).
- `exclude_claims: list[str]` — profile fields to drop before attaching.

`owner` / `repo` / `host` / `token` are exposed as **properties** that resolve
Soliplex `env:` / `secret:` interpolation markers lazily at tool-call time,
via `soliplex.config.interpolation.resolve_field`.

Each backing field declares its
[interpolation contract](https://soliplex.github.io/soliplex/config/interpolation/)
-- `env:` markers for `owner` / `repo` / `host`, `secret:` markers for
`token`, embedded anywhere within the value -- so `soliplex-cli audit`
reports a marker naming something the installation does not declare,
instead of leaving it to fail on the first tool call. Requires
Soliplex >= 0.80.

- `from_yaml(installation_config, config_path, config_dict)` *(classmethod)* —
  build the config from a room's YAML, mapping the public `owner` / `repo` /
  `host` / `token` keys onto the backing fields.
- `get_extra_parameters() -> dict` — the configured values surfaced for tool
  construction.

**`class RequestRoomToolConfig`** / **`class CheckRoomRequestToolConfig`**

Dataclasses describing the `request_room` and `check_room_request` tools for
a room. Configured keys:

- `service_url` — the room provisioner service's base URL (default
  `DEFAULT_PROVISIONER_URL`).
- `request_timeout` (`RequestRoomToolConfig` only) — seconds to wait for the
  service's answer to a room request (default `DEFAULT_REQUEST_ROOM_TIMEOUT`).

## `soliplex_concierge.labels`

Canonical Gitea request-label catalog, shared by the room tool and the admin
helpers. Each value is a `{"color": ..., "description": ...}` mapping.

- `ISSUE_TYPE_LABELS: dict[str, dict[str, str]]` — `new-room`, `room-access`.
- `DECISION_LABELS: dict[str, dict[str, str]]` — `approved`, `denied`.
- `REQUEST_LABELS: dict[str, dict[str, str]]` — the union of the two above.
- `LABEL_PAGE_SIZE` / `MAX_LABEL_PAGES` — paging constants for walking a
  repository's label listing.

## `soliplex_concierge.tls`

- `httpx_verify()` — return an httpx2 `verify` value honoring the OS trust store
  when the optional `truststore` package is installed (an
  `ssl.SSLContext`), otherwise `True` (httpx2's certifi default). See issue #46.

## `soliplex_concierge.tools.gitea`

The room-side tool that files a request as a Gitea issue.

**`class CreatedGiteaIssue(pydantic.BaseModel)`** — the created issue:
`number: int`, `url: str`, `title: str`, and `profile`, how the requesting
user's profile was recorded on it:

- `"attached"` — as the YAML attachment
- `"commented"` — as a comment holding the YAML, Gitea having refused the
  attachment
- `"missing"` — not at all, Gitea having refused the comment too

**`async create_gitea_issue(ctx, title, body, request_type) -> CreatedGiteaIssue`**

The tool the about-room calls to file a request. `ctx` is the pydantic-ai
`RunContext`; `request_type` is `"new-room"` or `"room-access"`. The target
repository is fixed by the room's `CreateGiteaIssueToolConfig`. It ensures the
type label exists — reusing the repository's existing label, found by walking
every page of its label listing, and creating one only when it is truly absent —
files the issue, attaches the requesting user's profile, and returns the issue
number and URL. Once the issue exists, recording the profile is best-effort:
when Gitea refuses the attachment, the profile is commented on the issue
instead, and the issue is returned either way.

**`create_gitea_issue_sync(*, host, token, owner, repo, user, title, body, request_type, profile_attachment_name="user-profile.yaml", exclude_claims=()) -> CreatedGiteaIssue`**

The synchronous counterpart of `create_gitea_issue`, for callers outside any
agent run (e.g. a room provisioner). What the tool reads from its run context
is passed in explicitly: the connection and profile settings its
`CreateGiteaIssueToolConfig` supplies, and the requesting `user` (a
`soliplex.models.UserProfile`, or a mapping of the user's claims). It files the
same issue the tool does — the two share one implementation — and raises the
same `UnknownRequestType` / `LabelNotApplied`.

**Exceptions**

- `UnknownRequestType(ValueError)` — `request_type` has no matching issue-type
  label.
- `AnonymousUser(ValueError)` — no user profile is attached to the run, so the
  request cannot be attributed.
- `LabelNotApplied(RuntimeError)` — the issue was created but its type label was
  not applied (typically the Gitea account lacks Write access; see issue #59).

## `soliplex_concierge.tools.provisioner`

The room-side tools that ask a stack's room provisioner service for a new
room, as the requesting user. Each call carries a service token holding the
user's claims (`service_claims(user)`: the profile's claims, less `iat` /
`exp` and `UserProfile`'s `"<unknown>"` placeholders), valid for
`SERVICE_TOKEN_LIFETIME` seconds and signed with the installation's
`URL_SAFE_TOKEN_SECRET`. The service's API is the
[Provisioner REST API](provisioner-rest-api.md): answers are parsed as its models,
and an answer naming another version of it fails with `IncompatibleService`.

**`async request_room(ctx, name, description=None, slug=None) -> RoomRequestAccepted`**

Sends `POST /rooms`. Returns `RoomRequestAccepted(status="accepted",
room_id, issue_url)` when the service is creating the room, and
`RoomRequestAccepted(status="not_admitted")` when it does not admit the user.
When the service does not answer within `request_timeout`, it asks once
more; a `409` naming the room (`"room_id"`) then returns
`RoomRequestAccepted(status="already_requested", room_id)`.

**`async check_room_request(ctx, room_id) -> RoomRequestState`**

Sends `GET /rooms/{room_id}`. Returns the request's `room_id`, `state`
(`pending`, `live`, `incomplete` or `failed`), `issue_url`, `incomplete`
steps and `error`. `RoomRequestState` is the API's `RequestState`.

**Exceptions** — reported to the model rather than ending the run:

- `BadSlug(pydantic_ai.ModelRetry)` — the slug the model passed is unusable.
- `BadRoomName(pydantic_ai.ToolFailed)` — the room name is unusable; ask the
  user for another.
- `RoomTaken(pydantic_ai.ToolFailed)` — the room id is taken, or already being
  provisioned.
- `UnknownRequest(pydantic_ai.ToolFailed)` — the service has no request for
  the room id.
- `ServiceUnavailable(pydantic_ai.ToolFailed)` /
  `ServiceFailed(pydantic_ai.ToolFailed)` — the service could not be reached,
  or failed (including an answer that does not parse as the API's model for
  it).
- `IncompatibleService(pydantic_ai.ToolFailed)` — the service's
  `X-Soliplex-Provisioner-API` header names another version of the API.
- `ServiceTimedOut(pydantic_ai.ToolFailed)` — the service did not answer a
  `check_room_request` in time.
- `RequestTimedOut(pydantic_ai.ToolFailed)` — the service answered neither a
  room request nor its retry; the room may still be created, so the message
  tells the model not to file an issue.
- `SigningSecretMissing(pydantic_ai.ToolFailed)` — the installation has no
  `URL_SAFE_TOKEN_SECRET`.
- `AnonymousUser(pydantic_ai.ToolFailed)` — no user profile is attached to the
  run.

The messages of `ServiceUnavailable`, `ServiceFailed`, `IncompatibleService`
and `SigningSecretMissing` tell the model to file the request as a `new-room`
issue instead.

## `soliplex_concierge.provisioner.api`

The [Provisioner REST API](provisioner-rest-api.md)'s contract: its models and
constants.

**Constants**

- `API_VERSION: str` (`"1"`) / `API_VERSION_HEADER: str`
  (`"X-Soliplex-Provisioner-API"`) — the API's version, and the response
  header that carries it.
- `SERVICE_TOKEN_SECRET: str` (`"URL_SAFE_TOKEN_SECRET"`) — the installation
  secret that signs the service tokens.
- `ADMISSION_ROOM_ID: str` (`"__provision__"`) — the reserved room id WRITE on
  which admits a requester.
- `ROOMS_PATH: str` (`"/rooms"`) / `HEALTH_PATH: str` (`"/ok"`) — the routes.

**Functions**

- `room_path(room_id) -> str` — the path of a room's request, the room id
  quoted as one segment.

**Models** (pydantic)

- `RoomRequest` — the body of `POST /rooms`: `name`, `description`, `slug`
  (no other fields).
- `Accepted` — the `202` answer: `room_id`, `issue_url`.
- `RequestState` — the `GET /rooms/{room_id}` answer: `room_id`, `state` (a
  `State`), `issue_url`, `incomplete`, `error`.
- `State(enum.StrEnum)` — `pending`, `live`, `incomplete`, `failed`.
- `Error` — an error answer: `detail`.
- `Refusal(Error)` — a `409` answer, naming its `room_id`.

## `soliplex_concierge.provisioner.contract`

Checks that an implementation keeps the API's contract (see
[Implementing a provisioner](../tasks/provisioners/index.md)).

- `Harness(typing.Protocol)` — the application under test (`app`), service
  tokens for its requesters (`admitted_token`, `other_token`, `admin_token`,
  `not_admitted_token`), and `occupy(name)`.
- `async check_all(harness)` — runs every check in `ALL_CHECKS`.
- `async check_health`, `check_requires_a_token`,
  `check_refuses_an_invalid_token`, `check_refuses_the_unadmitted`,
  `check_rejects_a_malformed_body`, `check_rejects_a_bad_slug`,
  `check_accepts_and_reports`, `check_hides_requests_from_others`,
  `check_reports_unknown_requests`, `check_refuses_an_existing_room` — each
  takes the harness.
- `ContractViolation(AssertionError)` — a broken contract;
  `MalformedAnswer(ContractViolation)` — an answer that does not parse as the
  API's model for it.

## `soliplex_concierge.provisioner.room_ids`

Ids of provisioned rooms (`<owner>:<slug>`), and their owners (see
[The owner and the requester](../tasks/provisioners/index.md#the-owner-and-the-requester)).

- `RoomOwner(owner, json_path)` — the id's owner part, and the access rule (a
  soliplex ACL JSONPath) granting the owner the room.
- `RoomId(owner, slug)` — an id, split; `str()` joins it.
- `owner_from_claims(claims, *, claim="preferred_username", flatten_value=False)`
  — the requester's own owner, its rule matching the claim's raw value.
- `named_owner(owner, json_path)` — any other owner, such as a group.
- `claim_json_path(claim, value)` — the rule matching users whose `claim` is
  `value`.
- `slugify(name)`, `validate_slug(slug)`, `make_room_id(owner, slug)`,
  `parse_room_id(room_id)`, `is_segment(value)`, `flatten(value)`.
- `RoomIdError` and its subclasses (`OwnerClaimMissing`, `BadOwner`,
  `BadOwnerClaim`, `BadOwnerPart`, `NoAccessRule`, `BadSlug`, `SlugEmpty`,
  `NotAProvisionedRoomId`) — an unusable owner, name, slug or id.

## `soliplex_concierge.provisioner.interfaces`

What the core hands its adapters, and what it asks of them (see
[What your adapters do](../tasks/provisioners/index.md#what-your-adapters-do)).

- `RoomRequest` — `requester_claims`, `owner`, `room_id`, `name`,
  `description`, `requested_at`; `.room` is the id as text.
- `Issue(typing.Protocol)` — a tracking issue; the core reads its `url`.
- `Undo(description, run)` — how to undo one step.
- `Provisioned` — a created room: `request`, `commit`, `issue`, `unfinished`.
- `RoomStore`, `DatabaseRegistrar`, `UploadsArea`, `Authz`, `Backend`,
  `IssueTracker` (each a `typing.Protocol`) — the adapters.
- `Admission(typing.Protocol)` — a service's `claims(token)`,
  `is_admitted(token)` and `is_admin(token)`.
- `InvalidToken(Exception)` — `Admission.claims` refuses a token;
  `AccessRefused(Exception)` — `Backend.check_access` finds the backend
  refusing the provisioner.

## `soliplex_concierge.provisioner.provision`

The steps of provisioning a room, and their guarantees (see
[What the core does](../tasks/provisioners/index.md#what-the-core-does)).

- `Provisioner(store, registrar, uploads, authz, backend, tracker=None,
  owner_claim="preferred_username", flatten_owner=False)` — a deployment's
  adapters.
- `plan_room(requester_claims, *, name, provisioner, description=None,
  slug=None, owner=None, now=None) -> RoomRequest` — changes nothing.
- `admit_room(request, provisioner) -> Admitted` — checks, then files the
  tracking issue; `Admitted(request, issue)`.
- `create_room(admitted, provisioner) -> Provisioned` — creates the room.
- `provision_room(requester_claims, *, name, provisioner, ...)` — all three.
- `check_free(provisioner, request)` — raises when the id, or anything named
  for it, is in use.
- `ProvisionError` and its subclasses: `RoomExists`, `ResourcesExist` (with
  the adapters' `found`), `ProvisioningFailed` (with the step's `error` and any
  `rollback_failures`); `BadRoomName(room_ids.RoomIdError)` for a blank name.

## `soliplex_concierge.provisioner.landing_clone`

A git clone holding provisioned rooms.

- `locked(path)` — the clone's writer's lock (a context manager).
- `ensure_clone(path, *, origin_url=None)` — make `path` a repository,
  pointing `origin` at `origin_url`.
- `commit_room(path, room_id, *, author_name, author_email, message,
  also=()) -> str` — commit the room's directory (and `also`), the requester
  as author.
- `push(path, *, branch="main", token=None, timeout=...)` — push to `origin`,
  a Gitea token traveling only in the environment.
- `LandingCloneError` and its subclasses (`GitMissing`, `GitFailed`,
  `NothingToCommit`, `PushFailed`, `PushTimedOut`).

## `soliplex_concierge.provisioner.ingester`

A `haiku.rag serve` ingester's database config, and its reload.

- `check_config(text, *, config_dir, fragments_dir)` — refuse a main config
  whose `databases_include` does not name `fragments_dir`, or which leaves a
  database unplaced.
- `fragment_texts(directory)` — the fragments, as haiku.rag reads them.
- `registrations(*texts) -> Registrations` — the databases the texts name,
  with their locations.
- `reload(base_url, token, *, timeout=120)` — `POST /reload`; returns the
  ingester's report.
- `IngesterError` and its subclasses: `UnusableConfig` (`UnparsableConfig`,
  `DuplicateKey`, `FragmentsNotIncluded`, `DatabaseUnplaced`),
  `IngesterUnreachable`, `ReloadTimedOut`, `ReloadRefused`, `ReloadFailed`.

## `soliplex_concierge.provisioner.soliplex_cli`

`soliplex-cli` (or a CLI offering the same commands) for room policies and
audits. Each function takes the command (`cli`) and the installation's path as
it sees it (`environment`).

- `run(cli, *command, stdin=None, ok_codes=(0,)) -> str`.
- `stale_policy_ids(cli, environment) -> set[str]` — policies for rooms the
  installation does not have.
- `owner_policy(room_id, json_path) -> str` — a private policy granting the
  owner's rule READ and WRITE.
- `set_policy(cli, environment, room_id, policy)`,
  `remove_policy(cli, environment, room_id)`, `audit_rooms(cli, environment)`.
- `is_admin(cli, environment, token) -> bool`,
  `may(cli, environment, token, room_id, permission) -> bool` — the
  `admin-users check` and `room-authz check` verdicts for a service token.
- `CliFailed(Exception)` — the command could not run, or failed;
  `NoVerdict(CliFailed)` — a check gave no verdict.

## `soliplex_concierge.provisioner.soliplex_api`

The backend's installation API, called as the provisioner.

- `read_secret(path) -> str` — the signing secret, stripped.
- `mint_service_token(secret, claims, *, lifetime=300) -> str`.
- `validate_service_token(secret, token) -> dict` — the token's claims; raises
  `InvalidServiceToken` (an `interfaces.InvalidToken`).
- `call(base_url, method, path, *, token=None, timeout=30) -> dict` — a call
  under `/api`.
- `check_access(base_url, *, token=None)` — raises unless the backend admits
  the token as an admin's.
- `reload_rooms(base_url, *, token=None) -> dict` — the rooms reload's delta;
  `loaded(delta, room_id)` says whether it loaded the room.
- `BackendError` and its subclasses: `SecretUnreadable`, `AccessDenied` (also
  an `interfaces.AccessRefused`), `RequestFailed`.

## `soliplex_concierge.provisioner.service`

The [Provisioner REST API](provisioner-rest-api.md), served over a
deployment's adapters (see
[Serving it](../tasks/provisioners/index.md#serving-it)).

- `create_app(provisioner, admission) -> fastapi.FastAPI` — the application;
  `app.state.access` records whether the backend admits the provisioner, and
  the lifespan starts `watch_access`.
- `watch_access(backend, access, *, sleep=time.sleep)` — check
  `backend.check_access` until it passes, every `ACCESS_RETRY` seconds while
  refused and `BACKEND_RETRY` while unreachable.
- `Access` — the watcher's record: `state() -> (admitted, problem)`,
  `update(admitted, problem=None)`.
- `Requests` / `Request` — the in-memory request registry.
- `requester_id(provisioner, claims)` — how the service recognizes a
  request's requester (their own owner part).

## `soliplex_concierge.gitea_admin`

The implementation behind the admin `gitea_issues.py` CLI. Every function talks
to the Gitea REST API over httpx2 and takes the connection as its leading
arguments — `host`, `token`, `owner`, `repo` — followed by the operation's own
parameters.

**Reads**

- `list_issues(host, token, owner, repo, state="open", labels=None) -> list[dict]`
  — request issues (excluding pull requests), optionally filtered by `labels`.
- `get_issue(host, token, owner, repo, number) -> dict` — one issue's full
  payload.

**Comments and state**

- `comment_issue(host, token, owner, repo, number, body) -> dict` — add a
  comment.
- `close_issue(host, token, owner, repo, number, body=None) -> dict` —
  optionally comment, then close the issue.

**Labels**

- `list_labels(host, token, owner, repo) -> list[dict]` — every label defined
  on the repository, walking Gitea's paginated listing.
- `create_label(host, token, owner, repo, name, color, description) -> dict` —
  create a label.
- `add_labels_to_issue(host, token, owner, repo, number, label_ids) -> list[dict]`
  — attach label ids to an issue.
- `init_labels(host, token, owner, repo) -> dict[str, str]` — create any missing
  request labels; returns `{name: "created" | "exists"}`.

**Decisions**

- `approve_issue(host, token, owner, repo, number, body=None) -> dict` — label
  the issue `approved`, record the decision, and close it.
- `deny_issue(host, token, owner, repo, number, body=None) -> dict` — label the
  issue `denied`, record the decision, and close it.

**Entry point**

- `main(argv=None) -> int` — the CLI entry point used by the `gitea_issues.py`
  shim.

## `soliplex_concierge.installer`

The implementation behind the installer skill's `install_concierge.py` shim
(see [Setup](../tasks/rooms/setup.md) and the [CLI reference](cli.md)). It wires
the extension into a `soliplex-template`-generated stack through idempotent,
individually-testable edits. It depends on `ruamel.yaml` and `soliplex-skills`
(runtime deps of `soliplex-concierge`); most users run the shim or the
`install-concierge` console script and never import these symbols.

**Entry points**

- `main(argv=None) -> int` — the CLI entry point, exposed as the
  `install-concierge` console script and delegated to by the
  `install_concierge.py` shim. The skill bundle's `assets/` directory is a
  required **positional** argument (the installed package doesn't ship the room
  template); the shim prepends its own bundled path. Resolves the stack and
  assets, runs `apply`, and returns a process exit code (`0` ok, `2` on
  `InstallerError`).
- `apply(stack, assets, room_skill, docs_skill, opts) -> dict[str, str]` — make
  every change idempotently against `stack`, returning a `{target: action}`
  map. `room_skill` / `docs_skill` are the resolved skill trees to install (or
  `None` to skip).

**`class Options`** *(keyword-only dataclass)* — one apply run's resolved
options: `room_id`, `pin`, `gitea_host`, `gitea_token`, `owner`, `repo`,
`local_gitea`, `with_truststore`, `force`, `dry_run`. When `local_gitea` is set,
`apply` skips the `.env` `GITEA_HOST` / `GITEA_ACCESS_TOKEN` write (the stack's
`scripts/init_gitea.py` owns those).

**`class InstallerError(Exception)`** — a problem applying the extension; `main`
maps it to exit code `2`. Built via message-constructing classmethods
(`not_a_stack`, `assets_missing`, `bad_skill_dir`, `bad_pyproject`,
`bad_dockerfile`, `bad_installation`, …).

**Published skills**

- `class PublishedSkill` *(frozen dataclass)* — a skill published as a
  GitHub-release tarball: `name`, `owner`, `repo`, `asset_tarball`,
  `pointer_tag`, `dir_flag` (the `--…-skill-dir` flag named in download-failure
  hints), `pointer_manifest`. Its `published` property adapts it to a
  `soliplex_skills.install.PublishedSkill` for downloading.
- `ROOM`, `DOCS` — the two `PublishedSkill` specs installed into a stack
  (`soliplex-concierge-room` and `soliplex-docs`).
- `resolve_published_skill(spec, override, version, opts, stack, ctx) -> Path | None`
  — locate a skill tree to install: the `override` local dir, else download the
  published release (via `soliplex_skills.install.download_skill`, mapping its
  failures to `InstallerError`) into a temp dir owned by the `contextlib.ExitStack`
  *ctx*. Returns `None` when nothing will be installed (dry run, or already
  present without `--force`).

**Stack / asset discovery**

- `resolve_stack(stack_dir) -> Path` — the resolved stack root, or raise if it
  is not a generated stack (see `STACK_MARKERS`).
- `resolve_assets(assets_dir) -> Path` — validate and return the skill-bundle
  `assets/` dir (room template + snippet).
- `compose_project_name(stack) -> str` / `default_room_id(stack) -> str` — the
  compose project name, and the default `about_<project>` room id.
- `has_local_gitea(stack) -> bool` — whether `docker-compose.yml` defines a local
  `gitea` service (by name or a `gitea/gitea*` image). When true (and not
  `--no-local-gitea`), `main` defaults the room's owner/repo to
  `LOCAL_GITEA_OWNER` / `LOCAL_GITEA_REPO`.

**Edits** — pure `(text, …) -> (new_text, action)` functions, so `--dry-run` is
just "compute, don't write"; `action` is `ADDED` or `UNCHANGED`:

- `add_pyproject_dep(text, pin=None, with_truststore=False)` — add the
  `soliplex-concierge` dependency to `backend/pyproject.toml`.
- `add_requirements_dep(text, pin=None, with_truststore=False)` — add it to
  `backend/requirements.txt`, which a stack built on a soliplex release image
  installs its extra packages from.
- `add_dockerfile_dep(text, pin=None, with_truststore=False)` — add it to the
  `backend/Dockerfile` `uv add` block, in an older stack without
  `backend/requirements.txt`.
- `merge_installation(text, room_id) -> (new_text, {target: action})` —
  surgically merge the six `installation.yaml` entries.
- `update_env(text, host, token)` — add the `GITEA_HOST` / `GITEA_ACCESS_TOKEN`
  lines.
- `stack_gitea_script(pin=None, with_truststore=False) -> str` — render the
  stack's `scripts/gitea_issues.py` admin shim text.

**File installs** — mutate the stack, returning `ADDED` / `UNCHANGED`:

- `install_room(assets, stack, opts)` — copy and rewire the about-room template.
- `install_skill(name, skill_src, stack, opts)` — copy a skill tree into the
  stack and defang it (via `soliplex_skills.install.defang_skill`).
- `install_gitea_script(stack, opts)` — write the admin `gitea_issues.py` CLI.

**Constants** — `ADDED` / `UNCHANGED` (action strings); `SKILL_NAME` and
`STACK_MARKERS`; the wiring constants `DIST`, `TOOL_CONFIG`, `GITEA_TOOL`,
`GITEA_HOST`, `GITEA_TOKEN_SECRET`, `ASSET_ROOM`; `DEFAULT_GITEA_HOST` /
`DEFAULT_GITEA_TOKEN`; `LOCAL_GITEA_OWNER` / `LOCAL_GITEA_REPO` (the service
account and repo `soliplex-template`'s `init_gitea.py` provisions, defaulted into
the room when a local Gitea is detected); and `GITEA_ADMIN_MIN` (the `soliplex-concierge` version
floor pinned into the generated `gitea_issues.py` when `--version` is omitted).
