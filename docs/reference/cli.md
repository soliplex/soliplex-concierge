# Command-line scripts

The repo ships two runnable CLI scripts, each bundled with a skill. Both are
[PEP 723](https://peps.python.org/pep-0723/) scripts: run them with `uv run`,
which provisions their dependencies from the inline metadata — no install step.
(Each skill also bundles a `skill_versions.py`; that one is documented with the
[`soliplex-skills` versions CLI](https://soliplex.github.io/soliplex-skills/mechanisms/versions-cli/),
not here.)

## `install_concierge.py` — install the extension

`skills/soliplex-concierge-installer/scripts/install_concierge.py` wires the
extension into a `soliplex-template`-generated stack, idempotently. See
[Setup](../tasks/rooms/setup.md) for the full walkthrough; this is the option
summary.

```sh
uv run scripts/install_concierge.py --stack-dir /path/to/stack \
    --owner <gitea-owner> --repo <gitea-repo> [options]
```

The same logic is also installed as the **`install-concierge`** console script
(`pip install soliplex-concierge`). It takes the options below, but — because
the installed package does not ship the room template — the skill bundle's
`assets/` directory is a required leading positional argument:

```sh
install-concierge /path/to/soliplex-concierge-installer/assets \
    --stack-dir /path/to/stack --owner <gitea-owner> --repo <gitea-repo>
```

The `uv run scripts/install_concierge.py` shim supplies that path from its own
bundle automatically, so you never pass it there.

| Option | Purpose |
| --- | --- |
| `--stack-dir` | stack root (default: current directory) |
| `--owner` / `--repo` | Gitea owner and repository for filed issues |
| `--gitea-host` / `--gitea-token` | fill real `.env` values instead of placeholders |
| `--no-local-gitea` | skip local-Gitea auto-detection; keep the external-Gitea defaults |
| `--version <X.Y>` | pin the `soliplex-concierge` dependency added to the stack (`latest` for newest; omitted warns about skew) |
| `--with-truststore` | install the `[truststore]` extra (verify TLS against the OS trust store; issue #46) |
| `--room-skill-version` / `--docs-skill-version` | published tag to install for the room / docs skill |
| `--room-skill-dir` / `--docs-skill-dir` | install the room / docs skill from a local directory |
| `--room-id` | room id to create (default `about_<compose-project-name>`) |
| `--force` | overwrite an existing room/skill |
| `--dry-run` | report changes without writing |

## `gitea_issues.py` — triage and resolve requests

`skills/soliplex-concierge-admin/scripts/gitea_issues.py` is a thin shim over
[`soliplex_concierge.gitea_admin`](api.md#soliplex_conciergegitea_admin). The
installer drops an identical copy into the stack at `scripts/gitea_issues.py`.
See [Administering requests](../tasks/admin/index.md) for the workflow; this is
the subcommand summary.

Every subcommand takes the connection options `--host` / `--token` (falling back
to `GITEA_HOST` / `GITEA_ACCESS_TOKEN`) and the required `--owner` / `--repo`.

```sh
uv run scripts/gitea_issues.py <command> --owner <owner> --repo <repo> [...]
```

| Command | Purpose |
| --- | --- |
| `init` | create the request labels on the repository |
| `list` | list request issues (`--state open\|closed\|all`, repeatable `--label`) |
| `search` | filter by `--type new-room\|room-access`, `--decision approved\|denied`, `--state`, `--label` |
| `show <number>` | print one issue's title, metadata, and body |
| `comment <number> --body <text>` | add a comment |
| `close <number> [--body <text>]` | optionally comment, then close |
| `approve <number> [--body <text>]` | label `approved`, record the decision, and close |
| `deny <number> [--body <text>]` | label `denied`, record the decision, and close |

Behind an enterprise / internal CA, add `--with truststore` to the `uv run`
invocation so TLS verifies against the OS trust store (issue #46).
