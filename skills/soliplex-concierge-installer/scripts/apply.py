#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["ruamel.yaml"]
# ///
"""Apply the 'soliplex-concierge' extension to a generated Soliplex stack.

This script is bundled in the 'soliplex-concierge-installer' skill and run via
'uv run scripts/apply.py' (uv provisions the 'ruamel.yaml' dependency from the
PEP 723 header above). It wires the extension into an existing
'soliplex-template'-generated installation, making the same seven idempotent
changes a human would otherwise make by hand:

1. add 'soliplex-concierge' to 'backend/pyproject.toml' dependencies,
2. add it to the 'backend/Dockerfile' 'uv add' block (the generated Dockerfile
   does 'uv init --bare' and ignores the pyproject deps, so both are needed),
3. merge six entries into 'backend/environment/installation.yaml'
   (meta.tool_configs, environment, secrets, two skill_configs --
   'soliplex-concierge-room' and 'soliplex-docs' -- and room_paths),
4. copy the 'about_soliplex' room template into 'rooms/<room_id>/' (renaming
   it -- directory, 'id:' and the room_paths entry -- to '<room_id>'),
5. download + copy the 'soliplex-concierge-room' and 'soliplex-docs'
   filesystem skills under 'skills/',
6. add GITEA_HOST / GITEA_ACCESS_TOKEN placeholders to '.env', and
7. write the admin 'gitea_issues.py' CLI (a thin shim over
   'soliplex_concierge.gitea_admin') into '<stack>/scripts/'.

The room template lives beside this script in the bundled 'assets/' directory
(resolved relative to __file__); the wiring encoded here mirrors
'assets/installation-snippet.yaml' (the human-readable reference) -- keep the
two in sync.

The edits are expressed as pure '(text|obj) -> (new, action)' functions so they
are individually testable and '--dry-run' is just "compute, do not write".
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.error as urllib_error
import urllib.parse as urllib_parse
import urllib.request as urllib_request

from ruamel.yaml import YAML

# --- the constants that define the wiring ---------------------------------

DIST = "soliplex-concierge"
TOOL_CONFIG = "soliplex_concierge.config.CreateGiteaIssueToolConfig"
GITEA_TOOL = "soliplex_concierge.tools.gitea.create_gitea_issue"
GITEA_HOST = "GITEA_HOST"
GITEA_TOKEN_SECRET = "GITEA_ACCESS_TOKEN"
ASSET_ROOM = "about_soliplex"

# Bundled assets ship beside this script under '<skill>/assets/' (this file is
# '<skill>/scripts/apply.py'); resolve them relative to __file__ so apply.py
# works from an unpacked release bundle with no checkout.
ASSETS = pathlib.Path(__file__).resolve().parent.parent / "assets"

# Neither filesystem skill installed into the stack is bundled here; each is
# its own published GitHub-release artifact, downloaded by default from its
# 'latest' pointer (whose 'latest.json' names the immutable build + sha256) or
# an explicit '--<x>-skill-version' tag. '--<x>-skill-dir' installs a local
# copy instead. This mirrors
# skills/soliplex-concierge-room/scripts/skill_versions.py; the shared logic is
# slated to move to the planned 'soliplex-skills' library.
_USER_AGENT = "soliplex-concierge-installer"
# Schemes _get will open: https for GitHub, file:// for local/testing tarballs.
_ALLOWED_SCHEMES = frozenset({"https", "file"})


@dataclasses.dataclass(frozen=True)
class PublishedSkill:
    """A filesystem skill published as a GitHub-release tarball.

    'pointer_tag' is a moving release whose 'pointer_manifest' (latest.json)
    names the current immutable build + sha256; 'asset_tarball' is the
    '<name>/...'-rooted tarball attached to each build. 'dir_flag' is the CLI
    flag a user passes to install a local copy instead (used in error hints).
    """

    name: str
    owner: str
    repo: str
    asset_tarball: str
    pointer_tag: str
    dir_flag: str
    pointer_manifest: str = "latest.json"

    @property
    def download_base(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/releases/download"


ROOM = PublishedSkill(
    name="soliplex-concierge-room",
    owner="soliplex",
    repo="soliplex-concierge",
    asset_tarball="soliplex-concierge-room-skill.tar.gz",
    pointer_tag="room-skill-latest",
    dir_flag="--room-skill-dir",
)
DOCS = PublishedSkill(
    name="soliplex-docs",
    owner="soliplex",
    repo="soliplex",
    asset_tarball="soliplex-docs-skill.tar.gz",
    pointer_tag="docs-latest",
    dir_flag="--docs-skill-dir",
)

# The concierge's own room skill name, used throughout the installation wiring.
SKILL_NAME = ROOM.name

DEFAULT_GITEA_HOST = "https://gitea.example.com"
DEFAULT_GITEA_TOKEN = "replace-me"

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
            f"bundled assets not found under {assets}: expected "
            f"'rooms/{ASSET_ROOM}/' beside this script (is the skill bundle "
            "intact?)"
        )

    @classmethod
    def _skill_download(
        cls, spec: PublishedSkill, reason: str
    ) -> InstallerError:
        return cls(
            f"could not download the '{spec.name}' skill ({reason}); pass "
            f"{spec.dir_flag} to install from a local copy instead"
        )

    @classmethod
    def skill_bad_scheme(
        cls, spec: PublishedSkill, url: str
    ) -> InstallerError:
        return cls._skill_download(spec, f"unsupported URL {url!r}")

    @classmethod
    def skill_http(
        cls, spec: PublishedSkill, url: str, code: int
    ) -> InstallerError:
        return cls._skill_download(spec, f"HTTP {code}: {url}")

    @classmethod
    def skill_unreachable(
        cls, spec: PublishedSkill, url: str, reason: object
    ) -> InstallerError:
        return cls._skill_download(spec, f"{reason}: {url}")

    @classmethod
    def skill_bad_manifest(
        cls, spec: PublishedSkill, url: str
    ) -> InstallerError:
        return cls._skill_download(spec, f"invalid manifest at {url}")

    @classmethod
    def skill_checksum(
        cls, spec: PublishedSkill, tag: str, expected: str, actual: str
    ) -> InstallerError:
        return cls._skill_download(
            spec,
            f"checksum mismatch for {tag}: expected {expected}, got {actual}",
        )

    @classmethod
    def skill_no_skill_md(
        cls, spec: PublishedSkill, tag: str
    ) -> InstallerError:
        return cls(
            f"the downloaded '{spec.name}' archive ({tag}) has no SKILL.md"
        )

    @classmethod
    def bad_skill_dir(
        cls, spec: PublishedSkill, path: pathlib.Path
    ) -> InstallerError:
        return cls(
            f"{spec.dir_flag} {path} is not a skill directory "
            "(no SKILL.md found)"
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
    pin: str | None = None
    gitea_host: str = DEFAULT_GITEA_HOST
    gitea_token: str = DEFAULT_GITEA_TOKEN
    owner: str | None = None
    repo: str | None = None
    with_truststore: bool = False
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


def resolve_assets() -> pathlib.Path:
    """Return the bundled 'assets/' dir (room template + snippet).

    The assets ship beside this script under '<skill>/assets/' (see ASSETS);
    this is a self-check that the skill bundle is intact.
    """
    if not (ASSETS / "rooms" / ASSET_ROOM).is_dir():
        raise InstallerError.assets_missing(ASSETS)
    return ASSETS


def _skill_installed(stack: pathlib.Path, name: str) -> bool:
    dst = stack / "backend" / "environment" / "skills" / name
    return dst.exists()


def resolve_published_skill(
    spec: PublishedSkill,
    override: str | None,
    version: str | None,
    opts: Options,
    stack: pathlib.Path,
    ctx: contextlib.ExitStack,
) -> pathlib.Path | None:
    """Locate the filesystem skill tree for 'spec' to install.

    With its '--<x>-skill-dir' override, use that directory (it must contain a
    SKILL.md). Otherwise download the published release (default: the spec's
    'latest' pointer; or the '--<x>-skill-version' tag) into a temp dir owned
    by 'ctx'.

    Returns None when no install will happen anyway -- a dry run, or the skill
    is already present and '--force' was not given -- so neither the network
    nor a temp dir is touched in those cases.
    """
    if override is not None:
        path = pathlib.Path(override).resolve()
        if not (path / "SKILL.md").is_file():
            raise InstallerError.bad_skill_dir(spec, path)
        return path
    if opts.dry_run or (_skill_installed(stack, spec.name) and not opts.force):
        return None
    dest = pathlib.Path(ctx.enter_context(tempfile.TemporaryDirectory()))
    return download_skill(spec, version, dest)


# --- published-skill download (mirrors the room skill's skill_versions.py) ---
#
# Stdlib only. Network access to GitHub is needed; set GITHUB_TOKEN / GH_TOKEN
# to raise the API rate limit. The shared logic is slated to move to the
# planned 'soliplex-skills' library.


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _get(
    url: str,
    spec: PublishedSkill,
    *,
    accept: str = "application/octet-stream",
) -> bytes:
    """Fetch 'url' (https or file://); map failures to InstallerError."""
    scheme = urllib_parse.urlsplit(url).scheme
    if scheme not in _ALLOWED_SCHEMES:
        raise InstallerError.skill_bad_scheme(spec, url)
    request = urllib_request.Request(url)
    request.add_header("User-Agent", _USER_AGENT)
    request.add_header("Accept", accept)
    token = _token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        # Scheme allowlist above bounds this to https/file (S310 mitigated).
        with urllib_request.urlopen(request) as response:  # noqa: S310
            return response.read()
    except urllib_error.HTTPError as exc:
        raise InstallerError.skill_http(spec, url, exc.code) from exc
    except urllib_error.URLError as exc:
        raise InstallerError.skill_unreachable(spec, url, exc.reason) from exc


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_pointer(spec: PublishedSkill) -> dict:
    """Return the spec's 'latest' pointer manifest (latest.json)."""
    url = f"{spec.download_base}/{spec.pointer_tag}/{spec.pointer_manifest}"
    try:
        return json.loads(_get(url, spec))
    except json.JSONDecodeError as exc:
        raise InstallerError.skill_bad_manifest(spec, url) from exc


def _resolve_target(
    spec: PublishedSkill, version: str | None
) -> tuple[str, str, str | None]:
    """Resolve to (tag, asset_url, sha256), expanding the default pointer.

    'version is None' reads the spec's 'latest' pointer (verified sha256); an
    explicit tag builds the asset URL by name (no sha256 to verify).
    """
    if version is None:
        pointer = _read_pointer(spec)
        return (
            pointer.get("tag", spec.pointer_tag),
            pointer["asset_url"],
            pointer.get("sha256"),
        )
    return (
        version,
        f"{spec.download_base}/{version}/{spec.asset_tarball}",
        None,
    )


def download_skill(
    spec: PublishedSkill, version: str | None, dest: pathlib.Path
) -> pathlib.Path:
    """Download + extract the spec's skill into 'dest'; return its root."""
    tag, asset_url, sha256 = _resolve_target(spec, version)
    tarball = dest / spec.asset_tarball
    tarball.write_bytes(_get(asset_url, spec))
    if sha256:
        actual = _sha256(tarball)
        if actual != sha256:
            raise InstallerError.skill_checksum(spec, tag, sha256, actual)
    extract = dest / "extract"
    extract.mkdir()
    with tarfile.open(tarball) as archive:
        archive.extractall(extract, filter="data")
    matches = list(extract.glob("*/SKILL.md"))
    if not matches:
        raise InstallerError.skill_no_skill_md(spec, tag)
    return matches[0].parent


def compose_project_name(stack: pathlib.Path) -> str:
    """The compose project name: 'name:' in the compose file, else dir name."""
    data = YAML(typ="safe").load((stack / "docker-compose.yml").read_text())
    if isinstance(data, dict) and data.get("name"):
        return str(data["name"])
    return stack.name


def default_room_id(stack: pathlib.Path) -> str:
    return f"about_{compose_project_name(stack)}"


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


def _dist_requirement(with_truststore: bool) -> str:
    """The distribution name, with the '[truststore]' extra when opted in."""
    return f"{DIST}[truststore]" if with_truststore else DIST


# The release that first ships 'soliplex_concierge.gitea_admin' (the module the
# stack 'gitea_issues.py' shim imports); used as the dependency floor when no
# '--version' pin is given.
GITEA_ADMIN_MIN = ">=0.6"


def stack_gitea_script(
    pin: str | None = None, with_truststore: bool = False
) -> str:
    """Return the text of the stack's 'scripts/gitea_issues.py' admin shim.

    A thin PEP 723 script that delegates to 'soliplex_concierge.gitea_admin';
    'uv run' provisions the dependency (with the '[truststore]' extra when
    'with_truststore', and the '--version' pin when given, else the
    GITEA_ADMIN_MIN floor) -- mirroring the admin skill's own shim.
    """
    dist = _dist_requirement(with_truststore)
    requirement = f"{dist} {pin}" if pin else f"{dist}{GITEA_ADMIN_MIN}"
    return f'''\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["{requirement}"]
# ///
"""Read and resolve Soliplex room-request issues on a Gitea repository.

Installed into this stack by the soliplex-concierge installer. Thin entry
point: 'uv run' provisions 'soliplex-concierge' from the metadata above, then
delegates to 'soliplex_concierge.gitea_admin'. Run with '--help' for the
subcommands (list / show / approve / deny / ...).
"""

import sys

from soliplex_concierge.gitea_admin import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
'''


def add_pyproject_dep(
    text: str, pin: str | None = None, with_truststore: bool = False
) -> tuple[str, str]:
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

    dist = _dist_requirement(with_truststore)
    requirement = f"{dist} {pin}" if pin else dist
    indent = _entry_indent(lines, open_idx)
    lines.insert(open_idx + 1, f'{indent}"{requirement}",\n')
    return "".join(lines), ADDED


def add_dockerfile_dep(
    text: str, pin: str | None = None, with_truststore: bool = False
) -> tuple[str, str]:
    """Add 'soliplex-concierge' to the Dockerfile 'uv add' block."""
    if DIST in text:
        return text, UNCHANGED

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        match = _UVADD_RE.match(line)
        if match:
            dist = _dist_requirement(with_truststore)
            token = f"{dist}{pin.replace(' ', '')}" if pin else dist
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
_HAS_DOCS_SKILL = re.compile(r'skill_name:\s*["\']?' + re.escape(DOCS.name))

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
_DOCS_SKILL_BLOCK = [
    f'  - skill_name: "{DOCS.name}"\n',
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
        "installation: skill_configs (docs)": _add_list_entry(
            lines,
            _SKILL_CONFIGS_RE,
            _HAS_DOCS_SKILL,
            _DOCS_SKILL_BLOCK,
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


def _patch_room_config(path: pathlib.Path, opts: Options) -> None:
    yaml = _yaml()
    data = yaml.load(path.read_text())
    data["id"] = opts.room_id
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
    src = assets / "rooms" / ASSET_ROOM
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _patch_room_config(dst / "room_config.yaml", opts)
    return ADDED


def install_skill(
    name: str,
    skill_src: pathlib.Path | None,
    stack: pathlib.Path,
    opts: Options,
) -> str:
    """Copy a whole filesystem skill tree (SKILL.md + the rest) into 'skills/'.

    'skill_src' is None only when no copy will happen (dry run, or already
    installed without --force) -- the guards below return before using it.
    """
    dst = stack / "backend" / "environment" / "skills" / name
    if dst.exists() and not opts.force:
        return UNCHANGED
    if opts.dry_run:
        return ADDED
    shutil.copytree(skill_src, dst, dirs_exist_ok=True)
    return ADDED


def install_gitea_script(stack: pathlib.Path, opts: Options) -> str:
    """Write the admin 'gitea_issues.py' CLI into the stack 'scripts/' dir."""
    dst = stack / "scripts" / "gitea_issues.py"
    if dst.exists() and not opts.force:
        return UNCHANGED
    if opts.dry_run:
        return ADDED
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(stack_gitea_script(opts.pin, opts.with_truststore))
    return ADDED


# --- orchestration ---------------------------------------------------------


def _write_if(
    path: pathlib.Path, new_text: str, action: str, dry_run: bool
) -> None:
    if action != UNCHANGED and not dry_run:
        path.write_text(new_text)


def apply(
    stack: pathlib.Path,
    assets: pathlib.Path,
    room_skill: pathlib.Path | None,
    docs_skill: pathlib.Path | None,
    opts: Options,
) -> dict[str, str]:
    """Apply every change idempotently; return per-target actions."""
    results: dict[str, str] = {}

    pyproject = stack / "backend" / "pyproject.toml"
    new, action = add_pyproject_dep(
        pyproject.read_text(), opts.pin, opts.with_truststore
    )
    _write_if(pyproject, new, action, opts.dry_run)
    results["backend/pyproject.toml"] = action

    dockerfile = stack / "backend" / "Dockerfile"
    new, action = add_dockerfile_dep(
        dockerfile.read_text(), opts.pin, opts.with_truststore
    )
    _write_if(dockerfile, new, action, opts.dry_run)
    results["backend/Dockerfile"] = action

    inst = stack / "backend" / "environment" / "installation.yaml"
    new, inst_results = merge_installation(inst.read_text(), opts.room_id)
    results.update(inst_results)
    changed = any(a != UNCHANGED for a in inst_results.values())
    _write_if(inst, new, ADDED if changed else UNCHANGED, opts.dry_run)

    results[f"rooms/{opts.room_id}"] = install_room(assets, stack, opts)
    results[f"skills/{ROOM.name}"] = install_skill(
        ROOM.name, room_skill, stack, opts
    )
    results[f"skills/{DOCS.name}"] = install_skill(
        DOCS.name, docs_skill, stack, opts
    )

    env = stack / ".env"
    new, action = update_env(
        env.read_text(), opts.gitea_host, opts.gitea_token
    )
    _write_if(env, new, action, opts.dry_run)
    results[".env"] = action

    results["scripts/gitea_issues.py"] = install_gitea_script(stack, opts)

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
        "--room-skill-version",
        default=None,
        help=(
            "published 'soliplex-concierge-room' tag to install (default: the "
            "'room-skill-latest' pointer); e.g. a rolling build or 'v0.4'"
        ),
    )
    parser.add_argument(
        "--room-skill-dir",
        default=None,
        help=(
            "install the 'soliplex-concierge-room' skill from this local "
            "directory instead of downloading a published release "
            "(offline / development)"
        ),
    )
    parser.add_argument(
        "--docs-skill-version",
        default=None,
        help=(
            "published 'soliplex-docs' tag to install (default: the "
            "'docs-latest' pointer); e.g. a rolling build or 'v0.69'"
        ),
    )
    parser.add_argument(
        "--docs-skill-dir",
        default=None,
        help=(
            "install the 'soliplex-docs' skill from this local directory "
            "instead of downloading a published release (offline / "
            "development)"
        ),
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
        "--with-truststore",
        action="store_true",
        help=(
            "add the 'soliplex-concierge[truststore]' extra so the "
            "create_gitea_issue tool verifies TLS against the OS trust store "
            "(an enterprise/internal CA) instead of certifi's bundle. Note: "
            "if the bare dependency is already present, the extra is not "
            "added on a re-run -- edit it by hand in that case."
        ),
    )
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
    print(
        "  - triage filed requests with: uv run "
        f"{stack / 'scripts' / 'gitea_issues.py'} list --owner <o> --repo <r>"
    )


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


def _warn_missing_owner_repo(owner: str | None, repo: str | None) -> None:
    """Warn when --owner/--repo are omitted (the room keeps placeholders)."""
    if owner is None or repo is None:
        print(
            "warning: --owner/--repo not given; the room keeps its "
            "placeholder owner/repo, so create_gitea_issue cannot file "
            "issues until you set them. Pass --owner and --repo, or edit "
            "the room's room_config.yaml.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        stack = resolve_stack(args.stack_dir)
        assets = resolve_assets()
        opts = Options(
            room_id=args.room_id or default_room_id(stack),
            pin=_pin_for_version(args.version),
            gitea_host=args.gitea_host,
            gitea_token=args.gitea_token,
            owner=args.owner,
            repo=args.repo,
            with_truststore=args.with_truststore,
            force=args.force,
            dry_run=args.dry_run,
        )
        _warn_missing_owner_repo(opts.owner, opts.repo)
        # Each skill may be downloaded into a temp dir; keep them alive until
        # apply() (install_skill) has copied them into the stack.
        with contextlib.ExitStack() as ctx:
            room_skill = resolve_published_skill(
                ROOM,
                args.room_skill_dir,
                args.room_skill_version,
                opts,
                stack,
                ctx,
            )
            docs_skill = resolve_published_skill(
                DOCS,
                args.docs_skill_dir,
                args.docs_skill_version,
                opts,
                stack,
                ctx,
            )
            results = apply(stack, assets, room_skill, docs_skill, opts)
    except InstallerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    else:
        _print_summary(results, opts, stack, args.dry_run)
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
