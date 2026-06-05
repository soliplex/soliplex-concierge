"""Apply the 'soliplex-concierge' extension to a generated Soliplex stack.

This module backs the 'soliplex-concierge-apply' console script. It wires the
extension into an existing 'soliplex-template'-generated installation, making
the same six idempotent changes a human would otherwise make by hand:

1. add 'soliplex-concierge' to 'backend/pyproject.toml' dependencies,
2. add it to the 'backend/Dockerfile' 'uv add' block (the generated Dockerfile
   does 'uv init --bare' and ignores the pyproject deps, so both are needed),
3. merge five entries into 'backend/environment/installation.yaml'
   (meta.tool_configs, environment, secrets, skill_configs, room_paths),
4. copy the 'about_soliplex' room template into 'rooms/<room_id>/' (renaming
   it -- directory, 'id:' and the room_paths entry -- to '<room_id>'),
5. copy the 'soliplex-concierge' filesystem skill under 'skills/', and
6. add GITEA_HOST / GITEA_ACCESS_TOKEN placeholders to '.env'.

The wiring encoded here mirrors 'example/installation-snippet.yaml' (the
human-readable reference); keep the two in sync.

The edits are expressed as pure '(text|obj) -> (new, action)' functions so they
are individually testable and '--dry-run' is just "compute, do not write".
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import pathlib
import re
import shutil
import sys
import tomllib
from importlib import metadata as _metadata

from ruamel.yaml import YAML

# --- the constants that define the wiring ---------------------------------

DIST = "soliplex-concierge"
TOOL_CONFIG = "soliplex_concierge.config.CreateGiteaIssueToolConfig"
GITEA_TOOL = "soliplex_concierge.tools.gitea.create_gitea_issue"
SKILL_NAME = "soliplex-concierge"
GITEA_HOST = "GITEA_HOST"
GITEA_TOKEN_SECRET = "GITEA_ACCESS_TOKEN"
ASSET_ROOM = "about_soliplex"
RAG_SKILL_KIND = "haiku.rag.skills.rag"

DEFAULT_GITEA_HOST = "https://gitea.example.com"
DEFAULT_GITEA_TOKEN = "replace-me"
# The RAG LanceDB stem to wire into the room when the stack has none to detect.
# It is the stem the soliplex-template haiku-ingester writes by default
# ('rag/db/haiku.rag.lancedb'), which the shipped asset's "rag" does NOT match.
DEFAULT_RAG_STEM = "haiku.rag"

# Files that mark a directory as a generated Soliplex stack.
STACK_MARKERS = (
    "docker-compose.yml",
    "backend/pyproject.toml",
    "backend/Dockerfile",
    "backend/environment/installation.yaml",
    ".env",
)

ADDED = "added"
UNCHANGED = "unchanged"

_CANON_DIST = re.sub(r"[-_.]+", "-", DIST).lower()
# Matches a requirement's leading distribution name; '*' so it always matches
# (an empty match for a name-less spec is fine -- it just won't compare equal).
_NAME_RE = re.compile(r"[A-Za-z0-9._-]*")
_DEPS_OPEN_RE = re.compile(r"^\s*dependencies\s*=\s*\[\s*$")
_UVADD_RE = re.compile(r"^(\s*)soliplex\s*\\\s*$")


class InstallerError(Exception):
    """A problem applying the extension; the CLI maps it to exit code 2."""

    @classmethod
    def not_a_stack(cls, stack: pathlib.Path, missing: str) -> InstallerError:
        return cls(
            f"{stack} is not a generated Soliplex stack: missing "
            f"'{missing}' (pass --stack-dir to point at the stack root)"
        )

    @classmethod
    def assets_missing(cls, assets: pathlib.Path) -> InstallerError:
        return cls(
            f"no extension assets under {assets}: expected 'example/' and "
            "'skill/' (run from a checkout or pass --assets-dir)"
        )

    @classmethod
    def bad_pyproject(cls) -> InstallerError:
        return cls(
            "could not find a 'dependencies = [' block in "
            "backend/pyproject.toml to extend"
        )

    @classmethod
    def bad_dockerfile(cls) -> InstallerError:
        return cls(
            "could not find the 'soliplex \\' line in the backend/Dockerfile "
            "'uv add' block to extend"
        )

    @classmethod
    def bad_installation(cls, section: str) -> InstallerError:
        return cls(
            f"could not find a '{section}:' block in "
            "backend/environment/installation.yaml to extend"
        )


@dataclasses.dataclass(kw_only=True)
class Options:
    """Resolved options for a single 'apply' run."""

    room_id: str
    rag_stem: str = DEFAULT_RAG_STEM
    pin: str | None = None
    gitea_host: str = DEFAULT_GITEA_HOST
    gitea_token: str = DEFAULT_GITEA_TOKEN
    owner: str | None = None
    repo: str | None = None
    force: bool = False
    dry_run: bool = False


# --- yaml helpers ----------------------------------------------------------


def _yaml() -> YAML:
    """A round-trip YAML configured to match the stack's layout."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _dump(yaml: YAML, data: object, path: pathlib.Path) -> None:
    buf = io.StringIO()
    yaml.dump(data, buf)
    path.write_text(buf.getvalue())


# --- stack / asset discovery ----------------------------------------------


def resolve_stack(stack_dir: str) -> pathlib.Path:
    """Return the resolved stack root, or raise if it is not a stack."""
    stack = pathlib.Path(stack_dir).resolve()
    for marker in STACK_MARKERS:
        if not (stack / marker).is_file():
            raise InstallerError.not_a_stack(stack, marker)
    return stack


def resolve_assets(override: str | None) -> pathlib.Path:
    """Locate the extension's 'example/' + 'skill/' asset directories.

    Defaults to the repository root (this file lives at
    'src/soliplex_concierge/installer.py'); '--assets-dir' overrides it.
    """
    if override is not None:
        assets = pathlib.Path(override).resolve()
    else:
        assets = pathlib.Path(__file__).resolve().parents[2]
    if not (assets / "example").is_dir() or not (assets / "skill").is_dir():
        raise InstallerError.assets_missing(assets)
    return assets


def installed_version() -> str | None:
    """Installed 'soliplex-concierge' version in this environment, if any."""
    try:
        return _metadata.version(DIST)
    except _metadata.PackageNotFoundError:
        return None


def compose_project_name(stack: pathlib.Path) -> str:
    """The compose project name: 'name:' in the compose file, else dir name."""
    data = YAML(typ="safe").load((stack / "docker-compose.yml").read_text())
    if isinstance(data, dict) and data.get("name"):
        return str(data["name"])
    return stack.name


def default_room_id(stack: pathlib.Path) -> str:
    return f"about_{compose_project_name(stack)}"


def detect_rag_stem(stack: pathlib.Path) -> str | None:
    """The stem of an existing RAG LanceDB under 'rag/db/', if any.

    Prefers the template's default 'haiku.rag' when present, else the sole
    database; returns None when 'rag/db/' has no '*.lancedb' (e.g. the
    ingester has not run yet).
    """
    db_dir = stack / "rag" / "db"
    stems = sorted(
        p.name[: -len(".lancedb")] for p in db_dir.glob("*.lancedb")
    )
    if not stems:
        return None
    return DEFAULT_RAG_STEM if DEFAULT_RAG_STEM in stems else stems[0]


def resolve_rag_stem(stack: pathlib.Path, override: str | None) -> str:
    """The RAG stem to wire: explicit override, else detected, else default."""
    return override or detect_rag_stem(stack) or DEFAULT_RAG_STEM


# --- pure text edits -------------------------------------------------------


def _dist_name(requirement: str) -> str:
    name = _NAME_RE.match(requirement.strip()).group(0)
    return re.sub(r"[-_.]+", "-", name).lower()


def _entry_indent(lines: list[str], open_idx: int) -> str:
    nxt = lines[open_idx + 1] if open_idx + 1 < len(lines) else ""
    stripped = nxt.strip()
    if stripped and stripped != "]":
        return nxt[: len(nxt) - len(nxt.lstrip())]
    return "    "


def add_pyproject_dep(text: str, pin: str | None = None) -> tuple[str, str]:
    """Add 'soliplex-concierge' to the '[project] dependencies' array."""
    data = tomllib.loads(text)
    deps = data.get("project", {}).get("dependencies", [])
    if any(_dist_name(dep) == _CANON_DIST for dep in deps):
        return text, UNCHANGED

    lines = text.splitlines(keepends=True)
    open_idx = next(
        (i for i, line in enumerate(lines) if _DEPS_OPEN_RE.match(line)),
        None,
    )
    if open_idx is None:
        raise InstallerError.bad_pyproject()

    requirement = f"{DIST} {pin}" if pin else DIST
    indent = _entry_indent(lines, open_idx)
    lines.insert(open_idx + 1, f'{indent}"{requirement}",\n')
    return "".join(lines), ADDED


def add_dockerfile_dep(text: str, pin: str | None = None) -> tuple[str, str]:
    """Add 'soliplex-concierge' to the Dockerfile 'uv add' block."""
    if DIST in text:
        return text, UNCHANGED

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        match = _UVADD_RE.match(line)
        if match:
            token = f"{DIST}{pin.replace(' ', '')}" if pin else DIST
            lines.insert(i + 1, f"{match.group(1)}{token} \\\n")
            return "".join(lines), ADDED
    raise InstallerError.bad_dockerfile()


def update_env(text: str, host: str, token: str) -> tuple[str, str]:
    """Append GITEA_HOST / GITEA_ACCESS_TOKEN unless already present."""
    if any(
        line.lstrip().startswith(f"{GITEA_HOST}=")
        for line in text.splitlines()
    ):
        return text, UNCHANGED

    sep = "\n" if text and not text.endswith("\n") else ""
    block = (
        f"{sep}\n"
        "# soliplex-concierge: Gitea endpoint + token read by the\n"
        "# 'create_gitea_issue' tool. Point these at your Gitea instance.\n"
        f"{GITEA_HOST}={host}\n"
        f"{GITEA_TOKEN_SECRET}={token}\n"
    )
    return text + block, ADDED


# --- installation.yaml merge ----------------------------------------------
#
# Surgical, line-based insertion (mirrors the soliplex-template skill's
# 'wire_room_stem'): each entry is inserted right after its 'key:' line, so
# nothing else in the file -- comments, indentation, unrelated sections such
# as 'agent_configs' or 'sandbox_config' -- is reformatted. A round-trip YAML
# load/dump would normalize the whole document; we deliberately avoid it here.

_META_RE = re.compile(r"^meta:\s*$")
_META_TOOL_CONFIGS_RE = re.compile(r"^(\s+)tool_configs:\s*$")
_TOP_KEY_RE = re.compile(r"^[^\s#]")  # a column-0 key (ends a nested block)

# Per-section: anchor (the 'key:' line), idempotency probe, and the block to
# insert (already indented, with trailing newlines).
_ENVIRONMENT_RE = re.compile(r"^environment:\s*$")
_SECRETS_RE = re.compile(r"^secrets:\s*$")
_SKILL_CONFIGS_RE = re.compile(r"^skill_configs:\s*$")
_ROOM_PATHS_RE = re.compile(r"^room_paths:\s*$")

_HAS_TOOL_CONFIG = re.compile(re.escape(TOOL_CONFIG))
_HAS_GITEA_HOST = re.compile(
    r'^\s*-\s*["\']?' + re.escape(GITEA_HOST) + r'["\']?\s*$'
)
_HAS_GITEA_SECRET = re.compile(
    r'secret_name:\s*["\']?' + re.escape(GITEA_TOKEN_SECRET)
)
_HAS_SKILL = re.compile(r'skill_name:\s*["\']?' + re.escape(SKILL_NAME))

_GITEA_HOST_BLOCK = [f'  - "{GITEA_HOST}"\n']
_GITEA_SECRET_BLOCK = [
    f'  - secret_name: "{GITEA_TOKEN_SECRET}"\n',
    "    sources:\n",
    '      - kind: "env_var"\n',
    f'        env_var_name: "{GITEA_TOKEN_SECRET}"\n',
]
_SKILL_BLOCK = [
    f'  - skill_name: "{SKILL_NAME}"\n',
    '    kind: "filesystem"\n',
]


def _find(lines: list[str], anchor: re.Pattern) -> int | None:
    return next(
        (i for i, line in enumerate(lines) if anchor.match(line)), None
    )


def _has(lines: list[str], probe: re.Pattern) -> bool:
    return any(probe.search(line) for line in lines)


def _add_list_entry(
    lines: list[str],
    anchor: re.Pattern,
    probe: re.Pattern,
    block: list[str],
    section: str,
) -> str:
    """Insert 'block' as the first item under a top-level 'key:' list."""
    if _has(lines, probe):
        return UNCHANGED
    idx = _find(lines, anchor)
    if idx is None:
        raise InstallerError.bad_installation(section)
    lines[idx + 1 : idx + 1] = block
    return ADDED


def _add_meta_tool_config(lines: list[str]) -> str:
    """Register the tool-config class under 'meta.tool_configs'.

    Appends to an existing 'tool_configs:' under 'meta:' when present,
    otherwise creates that nested block right after the 'meta:' line.
    """
    if _has(lines, _HAS_TOOL_CONFIG):
        return UNCHANGED
    meta_idx = _find(lines, _META_RE)
    if meta_idx is None:
        raise InstallerError.bad_installation("meta")
    item = f'- "{TOOL_CONFIG}"'
    for i in range(meta_idx + 1, len(lines)):
        if _TOP_KEY_RE.match(lines[i]):  # left the meta block
            break
        existing = _META_TOOL_CONFIGS_RE.match(lines[i])
        if existing:
            lines[i + 1 : i + 1] = [f"{existing.group(1)}  {item}\n"]
            return ADDED
    lines[meta_idx + 1 : meta_idx + 1] = [
        "  tool_configs:\n",
        f"    {item}\n",
    ]
    return ADDED


def merge_installation(text: str, room_id: str) -> tuple[str, dict[str, str]]:
    """Add the five extension entries to installation.yaml text, surgically."""
    lines = text.splitlines(keepends=True)
    entry = f"./rooms/{room_id}"
    room_probe = re.compile(r'-\s*["\']?' + re.escape(entry) + r'["\']?\s*$')
    results = {
        "installation: meta.tool_configs": _add_meta_tool_config(lines),
        "installation: environment": _add_list_entry(
            lines,
            _ENVIRONMENT_RE,
            _HAS_GITEA_HOST,
            _GITEA_HOST_BLOCK,
            "environment",
        ),
        "installation: secrets": _add_list_entry(
            lines,
            _SECRETS_RE,
            _HAS_GITEA_SECRET,
            _GITEA_SECRET_BLOCK,
            "secrets",
        ),
        "installation: skill_configs": _add_list_entry(
            lines,
            _SKILL_CONFIGS_RE,
            _HAS_SKILL,
            _SKILL_BLOCK,
            "skill_configs",
        ),
        "installation: room_paths": _add_list_entry(
            lines,
            _ROOM_PATHS_RE,
            room_probe,
            [f'  - "{entry}"\n'],
            "room_paths",
        ),
    }
    return "".join(lines), results


# --- file installs ---------------------------------------------------------


def _set_rag_stem(data: object, rag_stem: str) -> None:
    skills = data.get("skills")
    if not isinstance(skills, dict):
        return
    for skill in skills.get("skill_configs", []):
        if isinstance(skill, dict) and skill.get("kind") == RAG_SKILL_KIND:
            skill["rag_lancedb_stem"] = rag_stem


def _patch_room_config(path: pathlib.Path, opts: Options) -> None:
    yaml = _yaml()
    data = yaml.load(path.read_text())
    data["id"] = opts.room_id
    _set_rag_stem(data, opts.rag_stem)
    for tool in data.get("tools", []):
        if isinstance(tool, dict) and tool.get("tool_name") == GITEA_TOOL:
            if opts.owner is not None:
                tool["owner"] = opts.owner
            if opts.repo is not None:
                tool["repo"] = opts.repo
    _dump(yaml, data, path)


def install_room(
    assets: pathlib.Path, stack: pathlib.Path, opts: Options
) -> str:
    """Copy the room template to 'rooms/<room_id>/' and rename + rewire it."""
    dst = stack / "backend" / "environment" / "rooms" / opts.room_id
    if dst.exists() and not opts.force:
        return UNCHANGED
    if opts.dry_run:
        return ADDED
    src = assets / "example" / "rooms" / ASSET_ROOM
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _patch_room_config(dst / "room_config.yaml", opts)
    return ADDED


def install_skill(
    assets: pathlib.Path, stack: pathlib.Path, opts: Options
) -> str:
    """Copy the whole skill tree (SKILL.md + assets/) into 'skills/'."""
    dst = stack / "backend" / "environment" / "skills" / SKILL_NAME
    if dst.exists() and not opts.force:
        return UNCHANGED
    if opts.dry_run:
        return ADDED
    shutil.copytree(assets / "skill", dst, dirs_exist_ok=True)
    return ADDED


# --- orchestration ---------------------------------------------------------


def _write_if(
    path: pathlib.Path, new_text: str, action: str, dry_run: bool
) -> None:
    if action != UNCHANGED and not dry_run:
        path.write_text(new_text)


def apply(
    stack: pathlib.Path, assets: pathlib.Path, opts: Options
) -> dict[str, str]:
    """Apply every change idempotently; return per-target actions."""
    results: dict[str, str] = {}

    pyproject = stack / "backend" / "pyproject.toml"
    new, action = add_pyproject_dep(pyproject.read_text(), opts.pin)
    _write_if(pyproject, new, action, opts.dry_run)
    results["backend/pyproject.toml"] = action

    dockerfile = stack / "backend" / "Dockerfile"
    new, action = add_dockerfile_dep(dockerfile.read_text(), opts.pin)
    _write_if(dockerfile, new, action, opts.dry_run)
    results["backend/Dockerfile"] = action

    inst = stack / "backend" / "environment" / "installation.yaml"
    new, inst_results = merge_installation(inst.read_text(), opts.room_id)
    results.update(inst_results)
    changed = any(a != UNCHANGED for a in inst_results.values())
    _write_if(inst, new, ADDED if changed else UNCHANGED, opts.dry_run)

    results[f"rooms/{opts.room_id}"] = install_room(assets, stack, opts)
    results[f"skills/{SKILL_NAME}"] = install_skill(assets, stack, opts)

    env = stack / ".env"
    new, action = update_env(
        env.read_text(), opts.gitea_host, opts.gitea_token
    )
    _write_if(env, new, action, opts.dry_run)
    results[".env"] = action

    return results


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soliplex-concierge-apply",
        description=(
            "Wire the soliplex-concierge extension into an existing "
            "soliplex-template-generated stack."
        ),
    )
    parser.add_argument(
        "--stack-dir",
        default=".",
        help="path to the generated stack root (default: current directory)",
    )
    parser.add_argument(
        "--room-id",
        default=None,
        help="room id to install as (default: about_<compose-project-name>)",
    )
    parser.add_argument(
        "--rag-stem",
        default=None,
        help=(
            "RAG LanceDB stem to wire into the room's rag skill (default: the "
            "stack's existing rag/db/*.lancedb, else 'haiku.rag')"
        ),
    )
    parser.add_argument(
        "--assets-dir",
        default=None,
        help="override the extension checkout providing example/ and skill/",
    )
    parser.add_argument(
        "--version",
        default=None,
        help=(
            "version to pin soliplex-concierge to (e.g. '0.2'); 'latest' "
            "takes the newest release without a warning; omitting it warns "
            "and leaves the dependency unpinned"
        ),
    )
    parser.add_argument("--gitea-host", default=DEFAULT_GITEA_HOST)
    parser.add_argument("--gitea-token", default=DEFAULT_GITEA_TOKEN)
    parser.add_argument("--owner", default=None, help="Gitea repo owner")
    parser.add_argument("--repo", default=None, help="Gitea repo name")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the room/skill if they already exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the changes without writing anything",
    )
    return parser


def _print_summary(
    results: dict[str, str],
    opts: Options,
    stack: pathlib.Path,
    dry_run: bool,
) -> None:
    verb = "would apply" if dry_run else "applied"
    print(f"soliplex-concierge {verb} to {stack}:")
    for label, action in results.items():
        print(f"  - {label}: {action}")
    print()
    print("Next steps:")
    print(f"  - set {GITEA_HOST} / {GITEA_TOKEN_SECRET} in {stack / '.env'}")
    if opts.owner is None or opts.repo is None:
        print(
            "  - edit owner/repo on create_gitea_issue in "
            f"rooms/{opts.room_id}/room_config.yaml"
        )
    print("  - docker compose build backend && docker compose up -d")


def _pin_for_version(version: str | None) -> str | None:
    """Map the --version value to a pin spec, warning when it is omitted."""
    if version is None:
        print(
            "warning: --version not given; soliplex-concierge will be "
            "installed unpinned (the latest release), which risks version "
            "skew. Pass --version <X.Y> to pin, or --version latest to "
            "accept latest without this warning.",
            file=sys.stderr,
        )
        return None
    if version == "latest":
        return None
    return f"== {version}"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        stack = resolve_stack(args.stack_dir)
        assets = resolve_assets(args.assets_dir)
        opts = Options(
            room_id=args.room_id or default_room_id(stack),
            rag_stem=resolve_rag_stem(stack, args.rag_stem),
            pin=_pin_for_version(args.version),
            gitea_host=args.gitea_host,
            gitea_token=args.gitea_token,
            owner=args.owner,
            repo=args.repo,
            force=args.force,
            dry_run=args.dry_run,
        )
        results = apply(stack, assets, opts)
    except InstallerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    else:
        _print_summary(results, opts, stack, args.dry_run)
        version = installed_version()
        print(
            "installed soliplex-concierge version: "
            f"{version or 'not installed'}"
        )
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
