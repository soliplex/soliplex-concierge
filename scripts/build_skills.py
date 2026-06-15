#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["soliplex-skills>=0.5"]
# ///
"""Assemble + validate the soliplex-concierge skills into dist/.

The repo ships three hand-written skills under ``skills/``
(``soliplex-concierge-{installer,room,admin}``).

- Copy one (``--skill <name>``) or all of them into ``dist/<name>/``
- Stamp each ``SKILL.md`` with ``metadata.source_commit`` (from
  ``--commit``, default git HEAD)
- Validate each with the agent-skills reference library.

The heavy lifting is the shared ``soliplex_skills.build`` helper; this
script wraps it.

Packaging into release assets is the CI workflow's job;
``dist/`` is gitignored.

Run with uv (provisions ``soliplex-skills`` automatically):

    uv run scripts/build_skills.py --skill soliplex-concierge-room
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from soliplex_skills import build

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_DIR / "skills"
DIST = REPO_DIR / "dist"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble + validate concierge skills into dist/."
    )
    parser.add_argument(
        "--skill",
        help="skill dir under skills/ to build (default: all of them).",
    )
    parser.add_argument(
        "--commit",
        help="commit to stamp into SKILL.md metadata (default: git HEAD).",
    )
    parser.add_argument(
        "--version",
        help="Published version to stamp into SKILL.md. The concierge skills "
        "author metadata.version in their SKILL.md, which wins, so this is "
        "normally a no-op (omit for rolling builds).",
    )
    parser.add_argument(
        "--date",
        help="Build date (ISO YYYY-MM-DD) to stamp as 'generated' (default: "
        "today).",
    )
    args = parser.parse_args(argv)

    names = [args.skill] if args.skill else build.discover_skills(SKILLS_DIR)
    commit = args.commit or build.git_head_commit(REPO_DIR)
    for name in names:
        try:
            out = build.build_skill(
                name,
                src=SKILLS_DIR,
                dist=DIST,
                commit=commit,
                version=args.version,
                generated=args.date,
            )
        except (build.SkillNotFound, build.ValidationFailed) as exc:
            print(f"build_skills: error: {exc}", file=sys.stderr)
            return 1
        print(f"built & validated: {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
