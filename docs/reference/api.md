# Python API reference

The public symbols of the `soliplex_concierge` package. Most users never import
these directly — the room tool is wired in by configuration (see
[Setup](../tasks/rooms/setup.md)) and the admin functions are driven through the
`gitea_issues.py` CLI (see [Administering requests](../tasks/admin/index.md)).
The reference is for extension authors and for scripting against the admin
helpers.

## `soliplex_concierge.config`

Tool-configuration type registered with a Soliplex installation under
`meta.tool_configs`.

**Constants**

- `CGI_TOOL_NAME: str` — the dotted tool name,
  `"soliplex_concierge.tools.gitea.create_gitea_issue"`.
- `CGI_TOOL_KIND: str` — the tool kind (the final segment of `CGI_TOOL_NAME`).
- `DEFAULT_PROFILE_ATTACHMENT_NAME: str` — default filename for the attached
  user profile (`"user-profile.yaml"`).

**`class CreateGiteaIssueToolConfig(soliplex.config.tools.ToolConfig)`**

Dataclass describing the `create_gitea_issue` tool for a room. Configured keys:

- `owner`, `repo` — the tracking repository (required).
- `host` — Gitea base URL (default interpolation `"env:GITEA_HOST"`).
- `token` — access token (default interpolation `"secret:GITEA_ACCESS_TOKEN"`).
- `profile_attachment_name` — attachment filename (default
  `DEFAULT_PROFILE_ATTACHMENT_NAME`).
- `exclude_claims: list[str]` — profile fields to drop before attaching.

`owner` / `repo` / `host` / `token` are exposed as **properties** that resolve
Soliplex `env:` / `secret:` interpolation markers lazily at tool-call time.

- `from_yaml(installation_config, config_path, config_dict)` *(classmethod)* —
  build the config from a room's YAML, mapping the public `owner` / `repo` /
  `host` / `token` keys onto the backing fields.
- `get_extra_parameters() -> dict` — the configured values surfaced for tool
  construction.

## `soliplex_concierge.labels`

Canonical Gitea request-label catalog, shared by the room tool and the admin
helpers. Each value is a `{"color": ..., "description": ...}` mapping.

- `ISSUE_TYPE_LABELS: dict[str, dict[str, str]]` — `new-room`, `room-access`.
- `DECISION_LABELS: dict[str, dict[str, str]]` — `approved`, `denied`.
- `REQUEST_LABELS: dict[str, dict[str, str]]` — the union of the two above.

## `soliplex_concierge.tls`

- `httpx_verify()` — return an httpx `verify` value honoring the OS trust store
  when the optional `truststore` package is installed (an
  `ssl.SSLContext`), otherwise `True` (httpx's certifi default). See issue #46.

## `soliplex_concierge.tools.gitea`

The room-side tool that files a request as a Gitea issue.

**`class CreatedGiteaIssue(pydantic.BaseModel)`** — the created issue:
`number: int`, `url: str`, `title: str`.

**`async create_gitea_issue(ctx, title, body, request_type) -> CreatedGiteaIssue`**

The tool the about-room calls to file a request. `ctx` is the pydantic-ai
`RunContext`; `request_type` is `"new-room"` or `"room-access"`. The target
repository is fixed by the room's `CreateGiteaIssueToolConfig`. It ensures the
type label exists, files the issue, attaches the requesting user's profile, and
returns the issue number and URL.

**Exceptions**

- `UnknownRequestType(ValueError)` — `request_type` has no matching issue-type
  label.
- `AnonymousUser(ValueError)` — no user profile is attached to the run, so the
  request cannot be attributed.
- `LabelNotApplied(RuntimeError)` — the issue was created but its type label was
  not applied (typically the Gitea account lacks Write access; see issue #59).

## `soliplex_concierge.gitea_admin`

The implementation behind the admin `gitea_issues.py` CLI. Every function talks
to the Gitea REST API over httpx and takes the connection as its leading
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

- `list_labels(host, token, owner, repo) -> list[dict]` — labels defined on the
  repository.
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
