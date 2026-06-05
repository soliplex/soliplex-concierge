#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["skills-ref"]
# ///
"""Assemble and validate a soliplex-concierge skill into dist/.

The repo ships three hand-written skills under ``skills/``:

    soliplex-concierge-installer
    soliplex-concierge-room
    soliplex-concierge-admin

This copies one (``--skill <name>``) or all of them into the published skill
directory, e.g.::

    dist/soliplex-concierge-room/

and validates each with the agent-skills reference tool (``skills-ref``
package, ``agentskills`` CLI). Packaging into release assets (tarball/zip) is
the CI workflow's job (see .github/workflows/build-skills.yaml), which writes
those under ``dist/`` too. ``dist/`` is gitignored.

The assembled SKILL.md is stamped with ``metadata.source_commit`` (from
``--commit``, default git HEAD) so the bundled ``scripts/skill_versions.py``
can tell which published build is installed. The tracked source SKILL.md is
left unstamped -- only the published copy under dist/ carries the commit.

Run with uv (provisions the validator automatically):

    uv run scripts/build_skills.py --skill soliplex-concierge-room

or, without uv (falls back to ``uvx --from skills-ref agentskills``):

    python3 scripts/build_skills.py
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import typing

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_DIR / "skills"
DIST = REPO_DIR / "dist"


def die(msg: str) -> typing.NoReturn:
    print(f"build_skills: error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def discover_skills() -> list[str]:
    """Return the names of every skill dir (those with a SKILL.md)."""
    return sorted(
        path.name
        for path in SKILLS_DIR.iterdir()
        if (path / "SKILL.md").is_file()
    )


def git_head_commit() -> str | None:
    """Return the repo's current commit SHA, or None if unavailable."""
    if shutil.which("git") is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return out.stdout.strip() or None


def stamp_source_commit(skill_md: pathlib.Path, commit: str) -> None:
    """Record ``metadata.source_commit: "<commit>"`` in SKILL.md frontmatter.

    Mirrors the soliplex-template / soliplex-docs skills so
    scripts/skill_versions.py can identify the installed build. Inserts under
    an existing ``metadata:`` block if one is present, else appends a new block
    before the closing frontmatter fence.
    """
    lines = skill_md.read_text(encoding="utf-8").split("\n")
    fences = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(fences) < 2:
        die(f"{skill_md} has no YAML frontmatter to stamp")
    start, close = fences[0], fences[1]
    front = lines[start + 1 : close]
    if any(line.strip().startswith("source_commit:") for line in front):
        return  # already stamped
    entry = f'  source_commit: "{commit}"'
    meta_idx = next(
        (i for i, line in enumerate(front) if line.strip() == "metadata:"),
        None,
    )
    if meta_idx is not None:
        front.insert(meta_idx + 1, entry)
    else:
        front += ["metadata:", entry]
    lines[start + 1 : close] = front
    skill_md.write_text("\n".join(lines), encoding="utf-8")


def validator_cmd() -> list[str]:
    """Resolve how to invoke the agent-skills validator.

    Prefer the ``agentskills`` executable on PATH (present when this script is
    run via ``uv run``, which installs the PEP 723 ``skills-ref`` dependency);
    otherwise fall back to ``uvx --from skills-ref agentskills``.
    """
    exe = shutil.which("agentskills")
    if exe:
        return [exe, "validate"]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", "skills-ref", "agentskills", "validate"]
    die(
        "cannot find the agent-skills validator; install 'skills-ref' "
        "(pip install skills-ref) or run this script with 'uv run'"
    )


def build_skill(name: str, commit: str | None) -> pathlib.Path:
    """Assemble, stamp, and validate a single skill into dist/<name>/."""
    src = SKILLS_DIR / name
    if not (src / "SKILL.md").is_file():
        die(f"no skill named {name!r} under {SKILLS_DIR}")

    out = DIST / name
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__"))

    if commit:
        stamp_source_commit(out / "SKILL.md", commit)
    else:
        print(
            f"build_skills: warning: no commit available; "
            f"{name}/SKILL.md left unstamped",
            file=sys.stderr,
        )

    result = subprocess.run(validator_cmd() + [str(out)])
    if result.returncode != 0:
        die(f"skill validation failed for {name}")

    print(
        f"built & validated: {out}"
        + (f" (commit {commit[:7]})" if commit else "")
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble + validate concierge skills into dist/."
    )
    parser.add_argument(
        "--skill",
        help="skill dir under skills/ to build (default: all of them)",
    )
    parser.add_argument(
        "--commit",
        help="commit to stamp into SKILL.md metadata (default: git HEAD).",
    )
    args = parser.parse_args(argv)

    if not SKILLS_DIR.is_dir():
        die(f"skills dir not found: {SKILLS_DIR}")

    names = [args.skill] if args.skill else discover_skills()
    if not names:
        die(f"no skills found under {SKILLS_DIR}")

    commit = args.commit or git_head_commit()
    DIST.mkdir(exist_ok=True)
    for name in names:
        build_skill(name, commit)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
