"""Wire the 'soliplex-concierge' extension into a generated Soliplex stack.

This module backs the installer skill's 'install_concierge.py' shim (run via
'uv run scripts/install_concierge.py', which provisions 'soliplex-concierge'
and delegates here) and the 'install-concierge' console script. It makes the
same seven idempotent changes a human would otherwise make by hand:

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

The room template ships in the skill bundle's 'assets/' directory, beside the
shim -- not in this installed package -- so its location is a required
positional argument ('assets_dir'): the 'install_concierge.py' shim prepends
its own bundled path, and the 'install-concierge' console script takes it on
the command line. The wiring encoded here mirrors
'assets/installation-snippet.yaml' (the human-readable reference) -- keep the
two in sync.

The edits are expressed as pure '(text|obj) -> (new, action)' functions so they
are individually testable and '--dry-run' is just "compute, do not write".
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import pathlib
import re
import shutil
import sys
import tempfile
import tomllib

from ruamel.yaml import YAML
from soliplex_plumber import installation
from soliplex_plumber import rooms
from soliplex_plumber import sections
from soliplex_skills import install

# --- the constants that define the wiring ---------------------------------

DIST = "soliplex-concierge"
TOOL_CONFIG = "soliplex_concierge.config.CreateGiteaIssueToolConfig"
GITEA_TOOL = "soliplex_concierge.tools.gitea.create_gitea_issue"
GITEA_HOST = "GITEA_HOST"
GITEA_TOKEN_SECRET = "GITEA_ACCESS_TOKEN"
ASSET_ROOM = "about_soliplex"

# Neither filesystem skill installed into the stack is bundled here; each is
# its own published GitHub-release artifact, downloaded by default from its
# 'latest' pointer (whose 'latest.json' names the immutable build + sha256) or
# an explicit '--<x>-skill-version' tag. '--<x>-skill-dir' installs a local
# copy instead. The download/extract/verify and defang machinery now lives in
# the shared 'soliplex-skills' library (soliplex-skills#28); see
# resolve_published_skill and install_skill below.
_INSTALLED_BY = "soliplex-concierge-installer"


@dataclasses.dataclass(frozen=True)
class PublishedSkill:
    """A filesystem skill published as a GitHub-release tarball.

    Mirrors 'soliplex_skills.install.PublishedSkill' (the 'published' property
    adapts to it for downloading); 'dir_flag' is the extra CLI flag a user
    passes to install a local copy instead (used in error hints).
    """

    name: str
    owner: str
    repo: str
    asset_tarball: str
    pointer_tag: str
    dir_flag: str
    pointer_manifest: str = "latest.json"

    @property
    def published(self) -> install.PublishedSkill:
        """The library spec used to download this skill (drops 'dir_flag')."""
        return install.PublishedSkill(
            name=self.name,
            owner=self.owner,
            repo=self.repo,
            asset_tarball=self.asset_tarball,
            pointer_tag=self.pointer_tag,
            pointer_manifest=self.pointer_manifest,
        )


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

# When the target stack ships soliplex-template's local Gitea service (built
# with 'include_gitea'), its bundled 'scripts/init_gitea.py' provisions a fixed
# service account + tracking repo and writes GITEA_HOST / GITEA_ACCESS_TOKEN
# into '.env'. We default the room's owner/repo to match what that shim
# creates, rather than leaving placeholders. Keep these in sync with
# soliplex_template.gitea.ADMIN_USER / REPO_NAME -- concierge does not
# depend on soliplex-template, so they are duplicated here on purpose.
LOCAL_GITEA_OWNER = "soliplex-admin"
LOCAL_GITEA_REPO = "soliplex-requests"

# Files that mark a directory as a generated Soliplex stack: plumber's shared
# pair (docker-compose.yml + installation.yaml) plus the three the installer
# also edits.
STACK_MARKERS = (
    *sections.STACK_MARKERS,
    "backend/pyproject.toml",
    "backend/Dockerfile",
    ".env",
)

# The per-target action a step reports. Sourced from plumber (a StrEnum) so the
# installer and plumber's editors speak the same vocabulary.
ADDED = installation.TargetAction.ADDED
UNCHANGED = installation.TargetAction.UNCHANGED
COVERED = installation.TargetAction.COVERED

_CANON_DIST = re.sub(r"[-_.]+", "-", DIST).lower()
# Matches a requirement's leading distribution name; '*' so it always matches
# (an empty match for a name-less spec is fine -- it just won't compare equal).
_NAME_RE = re.compile(r"[A-Za-z0-9._-]*")
_DEPS_OPEN_RE = re.compile(r"^\s*dependencies\s*=\s*\[\s*$")
_UVADD_RE = re.compile(r"^(\s*)soliplex\s*\\\s*$")


class InstallerError(Exception):
    """A problem applying the extension; the CLI maps it to exit code 2."""


class NotAStack(InstallerError):
    def __init__(self, stack: pathlib.Path, missing: str):
        self.stack = stack
        self.missing = missing
        super().__init__(
            f"{stack} is not a generated Soliplex stack: missing "
            f"'{missing}' (pass --stack-dir to point at the stack root)"
        )


class AssetsMissing(InstallerError):
    def __init__(self, assets: pathlib.Path):
        self.assets = assets
        super().__init__(
            f"bundled assets not found under {assets}: expected "
            f"'rooms/{ASSET_ROOM}/' beside this script (is the skill bundle "
            "intact?)"
        )


class SkillDownloadFailed(InstallerError):
    def __init__(self, spec: PublishedSkill, reason: str):
        self.spec = spec
        self.reason = reason
        super().__init__(
            f"could not download the '{spec.name}' skill ({reason}); pass "
            f"{spec.dir_flag} to install from a local copy instead"
        )


class BadSkillDirectory(InstallerError):
    def __init__(self, spec: PublishedSkill, path: pathlib.Path):
        self.spec = spec
        self.path = path
        super().__init__(
            f"{spec.dir_flag} {path} is not a skill directory "
            "(no SKILL.md found)"
        )


class BadPyProject(InstallerError):
    def __init__(self):
        super().__init__(
            "could not find a 'dependencies = [' block in "
            "backend/pyproject.toml to extend"
        )


class BadDockerfile(InstallerError):
    def __init__(self):
        super().__init__(
            "could not find the 'soliplex \\' line in the backend/Dockerfile "
            "'uv add' block to extend"
        )


class SkillWhitelistActive(InstallerError):
    def __init__(self, kind: str, entries: list[str]):
        self.kind = kind
        self.entries = entries
        listed = ", ".join(entries) or "(none)"
        super().__init__(
            f"installation.yaml has an explicit '{kind}' skill_configs "
            f"whitelist ({listed}); re-run with --confirm-skill-whitelist to "
            "add the soliplex-concierge skills to it"
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
    local_gitea: bool = False
    with_truststore: bool = False
    confirm_skill_whitelist: bool = False
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
    return installation.resolve_stack(stack_dir, STACK_MARKERS, NotAStack)


def resolve_assets(assets_dir: pathlib.Path) -> pathlib.Path:
    """Return *assets_dir* (room template + snippet) after a sanity check.

    The assets ship in the skill bundle beside the 'install_concierge.py' shim,
    which passes their location here; this self-check confirms the bundle is
    intact.
    """
    if not (assets_dir / "rooms" / ASSET_ROOM).is_dir():
        raise AssetsMissing(assets_dir)
    return assets_dir


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
            raise BadSkillDirectory(spec, path)
        return path
    if opts.dry_run or (_skill_installed(stack, spec.name) and not opts.force):
        return None
    dest = pathlib.Path(ctx.enter_context(tempfile.TemporaryDirectory()))
    # Download + verify + extract is the shared library's job (soliplex-skills
    # #28); map its failures back to the installer's '--<x>-skill-dir' hint.
    try:
        return install.download_skill(spec.published, version, dest)
    except install.PointerUnavailable as exc:
        reason = f"could not read the '{spec.pointer_tag}' latest pointer"
        raise SkillDownloadFailed(spec, reason) from exc
    except (install.releases.GitHubAPIError, ValueError) as exc:
        raise SkillDownloadFailed(spec, str(exc)) from exc


def compose_project_name(stack: pathlib.Path) -> str:
    """The compose project name: 'name:' in the compose file, else dir name."""
    data = YAML(typ="safe").load((stack / "docker-compose.yml").read_text())
    if isinstance(data, dict) and data.get("name"):
        return str(data["name"])
    return stack.name


def default_room_id(stack: pathlib.Path) -> str:
    return f"about_{compose_project_name(stack)}"


def has_local_gitea(stack: pathlib.Path) -> bool:
    """True when the stack's compose file defines a local 'gitea' service.

    A stack generated by soliplex-template with 'include_gitea' ships a 'gitea'
    service (matched by name or a 'gitea/gitea*' image) plus a bundled
    'scripts/init_gitea.py' that provisions it. When present, the room can
    default to the repo that shim creates instead of carrying placeholders.
    """
    data = YAML(typ="safe").load((stack / "docker-compose.yml").read_text())
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return False
    for name, spec in services.items():
        if name == "gitea":
            return True
        image = spec.get("image") if isinstance(spec, dict) else None
        if isinstance(image, str) and image.startswith("gitea/gitea"):
            return True
    return False


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
        raise BadPyProject()

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
    raise BadDockerfile()


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
# The concierge-specific entries are composed over the stack's
# installation.yaml via plumber's section editors (comment-preserving,
# section-scoped). room_paths is *not* here -- it is wired by the room
# install (install_room_from). The
# 'skill_configs' entries go through the per-kind whitelist editor: on a
# permissive stack they are COVERED (the skills are auto-discovered under
# ./skills) and nothing is added; an explicit 'filesystem' whitelist makes
# plumber raise WhitelistActive, which we surface as a confirm-or-abort.


def merge_installation(
    text: str, *, confirm_skill_whitelist: bool = False
) -> tuple[str, dict[str, installation.TargetAction]]:
    """Add the concierge's installation.yaml entries to ``text``."""
    results: dict[str, installation.TargetAction] = {}
    try:
        text, results["installation: meta.tool_configs"] = (
            installation.add_meta_tool_config(text, TOOL_CONFIG)
        )
        text, results["installation: environment"] = (
            installation.add_environment(text, GITEA_HOST)
        )
        text, results["installation: secrets"] = installation.add_secret(
            text, GITEA_TOKEN_SECRET
        )
        text, results["installation: skill_configs"] = (
            installation.add_skill_config(
                text,
                SKILL_NAME,
                kind="filesystem",
                confirm=confirm_skill_whitelist,
            )
        )
        text, results["installation: skill_configs (docs)"] = (
            installation.add_skill_config(
                text,
                DOCS.name,
                kind="filesystem",
                confirm=confirm_skill_whitelist,
            )
        )
    except installation.WhitelistActive as exc:
        raise SkillWhitelistActive(kind=exc.kind, entries=exc.entries) from exc
    return text, results


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
) -> tuple[installation.TargetAction, installation.TargetAction]:
    """Copy the room template into the stack and wire its room_paths entry.

    Delegates the copytree + room_paths edit to plumber's ``install_room_from``
    (the about room always lives under ``./rooms``), then patches the copied
    ``room_config.yaml``. Returns ``(room-dir action, room_paths action)``.
    Catching ``RoomExists`` (not the broader ``AddRoomError``) keeps the
    install idempotent while letting a misconfigured parent surface as an
    error.
    """
    try:
        installed = rooms.install_room_from(
            stack,
            opts.room_id,
            assets / "rooms" / ASSET_ROOM,
            parent_path="./rooms",
            force=opts.force,
            dry_run=opts.dry_run,
        )
    except rooms.RoomExists:
        return UNCHANGED, UNCHANGED
    if not opts.dry_run:
        _patch_room_config(installed.config_path, opts)
    return ADDED, installed.path_action


def install_skill(
    name: str,
    skill_src: pathlib.Path | None,
    stack: pathlib.Path,
    opts: Options,
) -> str:
    """Copy a whole filesystem skill tree (SKILL.md + the rest) into 'skills/'.

    'skill_src' is None only when no copy will happen (dry run, or already
    installed without --force) -- the guards below return before using it.

    The copy is then *defanged* via 'soliplex_skills.install.defang_skill': a
    stack-installed skill is reachable by a Soliplex room agent and its users,
    who must never reach upgrade machinery that rewrites files and calls out to
    GitHub/PyPI, so its 'scripts/skill_versions.py' helper is removed and its
    SKILL.md self-management section is rewritten to an installer-managed note
    (naming '_INSTALLED_BY'). This was hoisted into the shared library in
    soliplex-skills#28.
    """
    dst = stack / "backend" / "environment" / "skills" / name
    if dst.exists() and not opts.force:
        return UNCHANGED
    if opts.dry_run:
        return ADDED
    shutil.copytree(skill_src, dst, dirs_exist_ok=True)
    install.defang_skill(dst, installed_by=_INSTALLED_BY)
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
    new, inst_results = merge_installation(
        inst.read_text(), confirm_skill_whitelist=opts.confirm_skill_whitelist
    )
    results.update(inst_results)
    changed = any(a == ADDED for a in inst_results.values())
    _write_if(inst, new, ADDED if changed else UNCHANGED, opts.dry_run)

    room_action, room_paths_action = install_room(assets, stack, opts)
    results[f"rooms/{opts.room_id}"] = room_action
    results["installation: room_paths"] = room_paths_action
    results[f"skills/{ROOM.name}"] = install_skill(
        ROOM.name, room_skill, stack, opts
    )
    results[f"skills/{DOCS.name}"] = install_skill(
        DOCS.name, docs_skill, stack, opts
    )

    # A stack with a local Gitea owns its '.env' GITEA_HOST /
    # GITEA_ACCESS_TOKEN via 'scripts/init_gitea.py' (run after
    # 'docker compose up -d'), so we leave them for that shim rather than
    # writing dead placeholders here.
    if opts.local_gitea:
        results[".env"] = UNCHANGED
    else:
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
        prog="install-concierge",
        description=(
            "Wire the soliplex-concierge extension into an existing "
            "soliplex-template-generated stack."
        ),
    )
    parser.add_argument(
        "assets_dir",
        type=pathlib.Path,
        help="the installer skill's bundled 'assets/' directory (the room "
        "template + installation snippet) to install from",
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
        "--no-local-gitea",
        action="store_true",
        help=(
            "do not auto-detect the stack's local Gitea service; keep the "
            "external-Gitea defaults (placeholder GITEA_HOST/token in '.env' "
            "and owner/repo in the room) for you to fill in by hand"
        ),
    )
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
        "--confirm-skill-whitelist",
        action="store_true",
        help=(
            "if installation.yaml already has an explicit filesystem "
            "'skill_configs' whitelist, add the soliplex-concierge skills to "
            "it (otherwise the install aborts rather than narrow your "
            "curated whitelist). On a stack without such a whitelist this "
            "has no effect -- the skills are auto-discovered."
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
    if opts.local_gitea:
        print(
            f"  - local Gitea detected: room wired to {opts.owner}/{opts.repo}"
        )
        print("  - docker compose build backend && docker compose up -d")
        print(
            "  - provision Gitea + populate "
            f"{GITEA_HOST} / {GITEA_TOKEN_SECRET} in {stack / '.env'} with: "
            "uv run scripts/init_gitea.py"
        )
        print("  - docker compose up -d backend  # pick up the new .env")
    else:
        print(
            f"  - set {GITEA_HOST} / {GITEA_TOKEN_SECRET} in {stack / '.env'}"
        )
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
        assets = resolve_assets(args.assets_dir)
        local_gitea = not args.no_local_gitea and has_local_gitea(stack)
        owner, repo = args.owner, args.repo
        if local_gitea:
            # Default to the service account + repo init_gitea.py provisions;
            # an explicit --owner/--repo still wins.
            owner = owner or LOCAL_GITEA_OWNER
            repo = repo or LOCAL_GITEA_REPO
        opts = Options(
            room_id=args.room_id or default_room_id(stack),
            pin=_pin_for_version(args.version),
            gitea_host=args.gitea_host,
            gitea_token=args.gitea_token,
            owner=owner,
            repo=repo,
            local_gitea=local_gitea,
            with_truststore=args.with_truststore,
            confirm_skill_whitelist=args.confirm_skill_whitelist,
            force=args.force,
            dry_run=args.dry_run,
        )
        if not local_gitea:
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
