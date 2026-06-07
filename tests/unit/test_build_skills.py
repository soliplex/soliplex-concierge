import importlib.util
import pathlib
from unittest import mock

import pytest
from soliplex_skills import build

# build_skills.py lives at the repo root under scripts/, outside the package;
# load it by path. Since the adoption of soliplex-skills it is a thin wrapper
# over soliplex_skills.build (discover -> build_skill); these tests pin that it
# delegates with the right arguments and translates the library's errors to a
# clean nonzero exit. The copy/stamp/validate is tested in soliplex-skills.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_skills.py"
_spec = importlib.util.spec_from_file_location("build_skills", SCRIPT)
build_skills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_skills)


@pytest.fixture
def built(monkeypatch):
    """Replace ``build.build_skill`` with a Mock and return it."""
    built = mock.Mock()
    monkeypatch.setattr(build_skills.build, "build_skill", built)
    return built


def test_main_builds_named_skill(built, monkeypatch):
    monkeypatch.setattr(
        build_skills.build, "git_head_commit", lambda repo: "feedface"
    )

    rc = build_skills.main(["--skill", "soliplex-concierge-room"])

    assert rc == 0
    built.assert_called_once_with(
        "soliplex-concierge-room",
        src=build_skills.SKILLS_DIR,
        dist=build_skills.DIST,
        commit="feedface",
    )


def test_main_builds_all_discovered(built, monkeypatch):
    monkeypatch.setattr(
        build_skills.build, "discover_skills", lambda skills_dir: ["a", "b"]
    )
    monkeypatch.setattr(
        build_skills.build, "git_head_commit", lambda repo: "feedface"
    )

    rc = build_skills.main([])

    assert rc == 0
    call_a, call_b = built.call_args_list
    assert call_a == mock.call(
        "a",
        src=build_skills.SKILLS_DIR,
        dist=build_skills.DIST,
        commit="feedface",
    )
    assert call_b == mock.call(
        "b",
        src=build_skills.SKILLS_DIR,
        dist=build_skills.DIST,
        commit="feedface",
    )


def test_main_threads_explicit_commit(built):
    rc = build_skills.main(
        ["--skill", "soliplex-concierge-admin", "--commit", "abc1234"]
    )

    assert rc == 0
    built.assert_called_once_with(
        "soliplex-concierge-admin",
        src=build_skills.SKILLS_DIR,
        dist=build_skills.DIST,
        commit="abc1234",
    )


def test_main_reports_build_error(built, monkeypatch, capsys):
    built.side_effect = build.ValidationFailed(
        "soliplex-concierge-room", ["bad frontmatter"]
    )
    monkeypatch.setattr(
        build_skills.build, "git_head_commit", lambda repo: "abc1234"
    )

    rc = build_skills.main(["--skill", "soliplex-concierge-room"])

    assert rc == 1
    assert "bad frontmatter" in capsys.readouterr().err
