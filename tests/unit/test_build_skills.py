import importlib.util
import pathlib

import pytest

from soliplex_concierge import installer

# build_skills.py lives at the repo root under scripts/, outside the package;
# load it by path so we can unit-test its pure logic.
REPO_ROOT = pathlib.Path(installer.__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_skills.py"
_spec = importlib.util.spec_from_file_location("build_skills", SCRIPT)
build_skills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_skills)

_FRONT_WITH_META = """\
---
name: demo-skill
description: |
    A demo.
license: MIT
metadata:
  version: "0.3.0"
---

# Demo
"""

_FRONT_NO_META = """\
---
name: demo-skill
description: A demo.
---

# Demo
"""


def test_stamp_source_commit_inserts_under_metadata(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(_FRONT_WITH_META)

    build_skills.stamp_source_commit(skill_md, "abc1234")

    text = skill_md.read_text()
    assert '  source_commit: "abc1234"' in text
    # Inserted as the first line under the existing 'metadata:' block.
    assert 'metadata:\n  source_commit: "abc1234"\n  version:' in text


def test_stamp_source_commit_appends_metadata_block(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(_FRONT_NO_META)

    build_skills.stamp_source_commit(skill_md, "abc1234")

    text = skill_md.read_text()
    assert 'metadata:\n  source_commit: "abc1234"' in text


def test_stamp_source_commit_is_idempotent(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(_FRONT_WITH_META)
    build_skills.stamp_source_commit(skill_md, "abc1234")

    build_skills.stamp_source_commit(skill_md, "deadbee")

    text = skill_md.read_text()
    assert "deadbee" not in text
    assert text.count("source_commit:") == 1


def test_stamp_source_commit_without_frontmatter_dies(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# no frontmatter here\n")

    with pytest.raises(SystemExit):
        build_skills.stamp_source_commit(skill_md, "abc1234")


def test_discover_skills_lists_the_three_skills():
    found = build_skills.discover_skills()

    assert found == [
        "soliplex-concierge-admin",
        "soliplex-concierge-installer",
        "soliplex-concierge-room",
    ]
