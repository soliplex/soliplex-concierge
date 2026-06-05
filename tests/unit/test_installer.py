import pathlib
import shutil
from importlib import metadata as _metadata

import pytest

from soliplex_concierge import installer

# The extension checkout (provides example/ + skill/): installer.py lives at
# 'src/soliplex_concierge/installer.py', so its parents[2] is the repo root.
REPO_ROOT = pathlib.Path(installer.__file__).resolve().parents[2]

_COMPOSE = "name: concierge-test\nservices: {}\n"

_PYPROJECT = """\
[project]
name = "soliplex-template"
version = "0.1.0"
dependencies = [
    "soliplex",
]
"""

_DOCKERFILE = """\
RUN \\
    uv init --bare && \\
    uv add \\
      --constraints constraints.txt \\
      soliplex \\
      psycopg[binary] && \\
    uv sync
"""

# 'meta:' is comment-only (no real tool_configs) to exercise the create
# branch. 'agent_configs' and the 4-space-indented 'sandbox_config' are
# unrelated sections used to prove the surgical merge never reformats them.
_INSTALLATION = """\
id: "concierge-test-conf"

meta:
  # nothing registered yet

agent_configs:
  - id: "default_chat"
    model_name: "gemma4:26b"

environment:
  - "OLLAMA_BASE_URL"

secrets:
  - secret_name: "URL_SAFE_TOKEN_SECRET"
    sources:
      - kind: "random_chars"

sandbox_config:
    environments_path: /sandbox/environments
    workdirs_path: /sandbox/workdirs

skill_configs:
  - skill_name: "bare-bones"
    kind: "filesystem"

room_paths:
  - "./rooms/chat"
"""

# Variant whose 'meta:' already has a real 'tool_configs:' list, to exercise
# the append-to-existing branch.
_INSTALLATION_META_TC = """\
meta:
  tool_configs:
    - "some.Existing.ToolConfig"

environment:
  - "OLLAMA_BASE_URL"

secrets:
  - secret_name: "URL_SAFE_TOKEN_SECRET"

skill_configs:
  - skill_name: "bare-bones"

room_paths:
  - "./rooms/chat"
"""

_ENV = "OLLAMA_BASE_URL=http://workshop:11434\nPUID=1000\n"


@pytest.fixture
def stack(temp_dir) -> pathlib.Path:
    """A minimal but realistic soliplex-template-generated stack."""
    env = temp_dir / "backend" / "environment"
    (env / "rooms").mkdir(parents=True)
    (env / "skills").mkdir()
    (temp_dir / "docker-compose.yml").write_text(_COMPOSE)
    (temp_dir / "backend" / "pyproject.toml").write_text(_PYPROJECT)
    (temp_dir / "backend" / "Dockerfile").write_text(_DOCKERFILE)
    (env / "installation.yaml").write_text(_INSTALLATION)
    (temp_dir / ".env").write_text(_ENV)
    return temp_dir


def _load(path: pathlib.Path):
    return installer._yaml().load(path.read_text())


# --- resolve_stack --------------------------------------------------------


def test_resolve_stack_ok(stack):
    result = installer.resolve_stack(str(stack))

    assert result == stack


@pytest.mark.parametrize("marker", installer.STACK_MARKERS)
def test_resolve_stack_missing_marker(stack, marker):
    (stack / marker).unlink()

    with pytest.raises(installer.InstallerError, match=marker):
        installer.resolve_stack(str(stack))


# --- resolve_assets -------------------------------------------------------


def test_resolve_assets_default_is_repo_root():
    result = installer.resolve_assets(None)

    assert result == REPO_ROOT


def test_resolve_assets_override(tmp_path):
    result = installer.resolve_assets(str(REPO_ROOT))

    assert result == REPO_ROOT


def test_resolve_assets_missing(temp_dir):
    with pytest.raises(installer.InstallerError, match="assets"):
        installer.resolve_assets(str(temp_dir))


# --- compose_project_name / default_room_id ------------------------------


@pytest.mark.parametrize(
    "compose,expected",
    [
        ("name: picked\nservices: {}\n", "picked"),
        ("services: {}\n", None),
        ("", None),
    ],
)
def test_compose_project_name(temp_dir, compose, expected):
    (temp_dir / "docker-compose.yml").write_text(compose)

    result = installer.compose_project_name(temp_dir)

    assert result == (expected if expected is not None else temp_dir.name)


def test_default_room_id(stack):
    result = installer.default_room_id(stack)

    assert result == "about_concierge-test"


# --- add_pyproject_dep ----------------------------------------------------


@pytest.mark.parametrize(
    "pin,expected",
    [
        (None, '"soliplex-concierge",'),
        ("== 0.2", '"soliplex-concierge == 0.2",'),
    ],
)
def test_add_pyproject_dep_added(pin, expected):
    new_text, action = installer.add_pyproject_dep(_PYPROJECT, pin)

    assert action == installer.ADDED
    assert expected in new_text


def test_add_pyproject_dep_unchanged():
    text = _PYPROJECT.replace(
        '    "soliplex",\n', '    "soliplex",\n    "soliplex-concierge",\n'
    )

    new_text, action = installer.add_pyproject_dep(text)

    assert action == installer.UNCHANGED
    assert new_text == text


def test_add_pyproject_dep_empty_array_indent():
    text = "[project]\ndependencies = [\n]\n"

    new_text, action = installer.add_pyproject_dep(text)

    assert action == installer.ADDED
    assert '    "soliplex-concierge",\n' in new_text


def test_add_pyproject_dep_bad():
    text = '[project]\ndependencies = ["soliplex"]\n'

    with pytest.raises(installer.InstallerError, match="dependencies"):
        installer.add_pyproject_dep(text)


# --- add_dockerfile_dep ---------------------------------------------------


@pytest.mark.parametrize(
    "pin,expected",
    [
        (None, "soliplex-concierge \\"),
        ("== 0.2", "soliplex-concierge==0.2 \\"),
    ],
)
def test_add_dockerfile_dep_added(pin, expected):
    new_text, action = installer.add_dockerfile_dep(_DOCKERFILE, pin)

    assert action == installer.ADDED
    assert expected in new_text


def test_add_dockerfile_dep_unchanged():
    text = _DOCKERFILE.replace(
        "      soliplex \\\n",
        "      soliplex \\\n      soliplex-concierge \\\n",
    )

    new_text, action = installer.add_dockerfile_dep(text)

    assert action == installer.UNCHANGED
    assert new_text == text


def test_add_dockerfile_dep_bad():
    with pytest.raises(installer.InstallerError, match="Dockerfile"):
        installer.add_dockerfile_dep("RUN echo no uv add here\n")


# --- update_env -----------------------------------------------------------


@pytest.mark.parametrize("base", ["X=1\n", "X=1"])
def test_update_env_added(base):
    new_text, action = installer.update_env(base, "https://g", "tok")

    assert action == installer.ADDED
    assert "GITEA_HOST=https://g" in new_text
    assert "GITEA_ACCESS_TOKEN=tok" in new_text


def test_update_env_unchanged():
    text = "GITEA_HOST=already\n"

    new_text, action = installer.update_env(text, "https://g", "tok")

    assert action == installer.UNCHANGED
    assert new_text == text


# --- merge_installation ---------------------------------------------------


def _loads(text):
    return installer._yaml().load(text)


def test_merge_installation_adds():
    new_text, results = installer.merge_installation(
        _INSTALLATION, "about_concierge-test"
    )

    assert set(results.values()) == {installer.ADDED}
    data = _loads(new_text)
    assert installer.TOOL_CONFIG in data["meta"]["tool_configs"]
    assert installer.GITEA_HOST in data["environment"]
    assert any(
        s.get("secret_name") == installer.GITEA_TOKEN_SECRET
        for s in data["secrets"]
    )
    assert any(
        s.get("skill_name") == installer.SKILL_NAME
        for s in data["skill_configs"]
    )
    assert "./rooms/about_concierge-test" in data["room_paths"]


def test_merge_installation_leaves_other_sections_verbatim():
    new_text, _ = installer.merge_installation(_INSTALLATION, "about_x")

    # Unrelated sections are byte-identical -- no reindent, no reflow.
    assert '  - id: "default_chat"\n    model_name: "gemma4:26b"' in new_text
    assert (
        "sandbox_config:\n"
        "    environments_path: /sandbox/environments\n"
        "    workdirs_path: /sandbox/workdirs" in new_text
    )
    # Pre-existing list items are preserved alongside the new ones.
    assert '  - skill_name: "bare-bones"' in new_text
    assert '  - "OLLAMA_BASE_URL"' in new_text


def test_merge_installation_idempotent():
    once, _ = installer.merge_installation(_INSTALLATION, "about_x")

    twice, results = installer.merge_installation(once, "about_x")

    assert set(results.values()) == {installer.UNCHANGED}
    assert twice == once


def test_add_meta_tool_config_creates_when_meta_ends_file():
    lines = ["meta:\n", "  # only a comment\n"]

    action = installer._add_meta_tool_config(lines)

    assert action == installer.ADDED
    assert "  tool_configs:\n" in lines


def test_merge_installation_appends_to_existing_tool_configs():
    new_text, results = installer.merge_installation(
        _INSTALLATION_META_TC, "about_x"
    )

    tcs = _loads(new_text)["meta"]["tool_configs"]
    assert results["installation: meta.tool_configs"] == installer.ADDED
    assert installer.TOOL_CONFIG in tcs
    assert "some.Existing.ToolConfig" in tcs


@pytest.mark.parametrize(
    "anchor,section",
    [
        ("meta:", "meta"),
        ("environment:", "environment"),
        ("secrets:", "secrets"),
        ("skill_configs:", "skill_configs"),
        ("room_paths:", "room_paths"),
    ],
)
def test_merge_installation_missing_section(anchor, section):
    text = "\n".join(
        line
        for line in _INSTALLATION.splitlines()
        if not line.startswith(anchor)
    )

    with pytest.raises(installer.InstallerError, match=section):
        installer.merge_installation(text, "about_x")


# --- _patch_room_config ---------------------------------------------------


@pytest.mark.parametrize(
    "owner,repo",
    [(None, None), ("acme", "reqs"), ("acme", None), (None, "reqs")],
)
def test_patch_room_config(temp_dir, owner, repo):
    src = REPO_ROOT / "example" / "rooms" / installer.ASSET_ROOM
    room = temp_dir / "room"
    shutil.copytree(src, room)
    cfg = room / "room_config.yaml"
    opts = _opts(room_id="about_acme", owner=owner, repo=repo, rag_stem="hr")

    installer._patch_room_config(cfg, opts)

    data = _load(cfg)
    assert data["id"] == "about_acme"
    rag = next(
        s
        for s in data["skills"]["skill_configs"]
        if s.get("kind") == installer.RAG_SKILL_KIND
    )
    assert rag["rag_lancedb_stem"] == "hr"
    tool = next(
        t for t in data["tools"] if t.get("tool_name") == installer.GITEA_TOOL
    )
    assert tool["owner"] == (
        owner if owner is not None else "your-gitea-owner"
    )
    assert tool["repo"] == (repo if repo is not None else "soliplex-requests")


# --- rag stem detection / resolution --------------------------------------


def test_detect_rag_stem_none(stack):
    result = installer.detect_rag_stem(stack)

    assert result is None


def test_detect_rag_stem_single(stack):
    (stack / "rag" / "db" / "mydb.lancedb").mkdir(parents=True)

    result = installer.detect_rag_stem(stack)

    assert result == "mydb"


def test_detect_rag_stem_prefers_haiku(stack):
    db = stack / "rag" / "db"
    db.mkdir(parents=True)
    (db / "other.lancedb").mkdir()
    (db / "haiku.rag.lancedb").mkdir()

    result = installer.detect_rag_stem(stack)

    assert result == "haiku.rag"


def test_resolve_rag_stem_override(stack):
    result = installer.resolve_rag_stem(stack, "explicit")

    assert result == "explicit"


def test_resolve_rag_stem_detected(stack):
    (stack / "rag" / "db" / "found.lancedb").mkdir(parents=True)

    result = installer.resolve_rag_stem(stack, None)

    assert result == "found"


def test_resolve_rag_stem_default_when_none(stack):
    result = installer.resolve_rag_stem(stack, None)

    assert result == installer.DEFAULT_RAG_STEM


def test_set_rag_stem_no_skills_dict():
    data = {"skills": None}

    installer._set_rag_stem(data, "x")

    assert data == {"skills": None}


def test_set_rag_stem_skips_non_matching():
    data = {"skills": {"skill_configs": [{"kind": "other"}, "notadict"]}}

    installer._set_rag_stem(data, "stem")

    assert "rag_lancedb_stem" not in data["skills"]["skill_configs"][0]


# --- install_room / install_skill -----------------------------------------


def _opts(**kw):
    kw.setdefault("room_id", "about_concierge-test")
    return installer.Options(**kw)


def test_install_room_added(stack):
    action = installer.install_room(REPO_ROOT, stack, _opts())

    rooms = stack / "backend" / "environment" / "rooms"
    cfg = _load(rooms / "about_concierge-test" / "room_config.yaml")
    assert action == installer.ADDED
    assert cfg["id"] == "about_concierge-test"


def test_install_room_unchanged(stack):
    rooms = stack / "backend" / "environment" / "rooms"
    (rooms / "about_concierge-test").mkdir()

    action = installer.install_room(REPO_ROOT, stack, _opts())

    assert action == installer.UNCHANGED


def test_install_room_dry_run(stack):
    action = installer.install_room(REPO_ROOT, stack, _opts(dry_run=True))

    rooms = stack / "backend" / "environment" / "rooms"
    assert action == installer.ADDED
    assert not (rooms / "about_concierge-test").exists()


def test_install_room_force(stack):
    rooms = stack / "backend" / "environment" / "rooms"
    (rooms / "about_concierge-test").mkdir()

    action = installer.install_room(REPO_ROOT, stack, _opts(force=True))

    assert action == installer.ADDED
    assert (rooms / "about_concierge-test" / "room_config.yaml").is_file()


def test_install_skill_added(stack):
    action = installer.install_skill(REPO_ROOT, stack, _opts())

    skill_dir = (
        stack / "backend" / "environment" / "skills" / installer.SKILL_NAME
    )
    assert action == installer.ADDED
    assert (skill_dir / "SKILL.md").is_file()
    # the whole tree is copied, including the request templates
    assert (skill_dir / "assets" / "room_creation_request.md").is_file()
    assert (skill_dir / "assets" / "room_access_request.md").is_file()


def test_install_skill_unchanged(stack):
    skill_dir = (
        stack / "backend" / "environment" / "skills" / installer.SKILL_NAME
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old")

    action = installer.install_skill(REPO_ROOT, stack, _opts())

    assert action == installer.UNCHANGED


def test_install_skill_dry_run(stack):
    action = installer.install_skill(REPO_ROOT, stack, _opts(dry_run=True))

    skill = (
        stack
        / "backend"
        / "environment"
        / "skills"
        / installer.SKILL_NAME
        / "SKILL.md"
    )
    assert action == installer.ADDED
    assert not skill.exists()


def test_install_skill_force(stack):
    skill_dir = (
        stack / "backend" / "environment" / "skills" / installer.SKILL_NAME
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old")

    action = installer.install_skill(REPO_ROOT, stack, _opts(force=True))

    assert action == installer.ADDED
    assert (skill_dir / "SKILL.md").read_text() != "old"


# --- main / apply end-to-end ----------------------------------------------


def test_main_applies(stack, capsys):
    rc = installer.main(
        ["--stack-dir", str(stack), "--assets-dir", str(REPO_ROOT)]
    )

    assert rc == 0
    backend = stack / "backend"
    assert "soliplex-concierge" in (backend / "pyproject.toml").read_text()
    assert "soliplex-concierge" in (backend / "Dockerfile").read_text()
    inst = _load(backend / "environment" / "installation.yaml")
    assert installer.GITEA_HOST in inst["environment"]
    assert (
        backend
        / "environment"
        / "rooms"
        / "about_concierge-test"
        / "room_config.yaml"
    ).is_file()
    assert (
        backend / "environment" / "skills" / installer.SKILL_NAME / "SKILL.md"
    ).is_file()
    assert "GITEA_HOST=" in (stack / ".env").read_text()
    assert "edit owner/repo" in capsys.readouterr().out


def test_main_dry_run(stack, capsys):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--dry-run",
        ]
    )

    assert rc == 0
    backend = stack / "backend"
    assert (backend / "pyproject.toml").read_text() == _PYPROJECT
    assert (stack / ".env").read_text() == _ENV
    assert not (
        backend / "environment" / "rooms" / "about_concierge-test"
    ).exists()
    assert "would apply" in capsys.readouterr().out


def test_main_idempotent(stack):
    opts = _opts()
    installer.apply(stack, REPO_ROOT, opts)

    results = installer.apply(stack, REPO_ROOT, opts)

    assert set(results.values()) == {installer.UNCHANGED}


def test_main_room_id_override(stack):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--room-id",
            "custom_room",
        ]
    )

    assert rc == 0
    assert (
        stack / "backend" / "environment" / "rooms" / "custom_room"
    ).is_dir()


def test_main_with_owner_repo(stack, capsys):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--owner",
            "acme",
            "--repo",
            "reqs",
        ]
    )

    assert rc == 0
    cfg = _load(
        stack
        / "backend"
        / "environment"
        / "rooms"
        / "about_concierge-test"
        / "room_config.yaml"
    )
    tool = next(
        t for t in cfg["tools"] if t.get("tool_name") == installer.GITEA_TOOL
    )
    assert (tool["owner"], tool["repo"]) == ("acme", "reqs")
    assert "edit owner/repo" not in capsys.readouterr().out


def test_main_not_a_stack(temp_dir, capsys):
    rc = installer.main(["--stack-dir", str(temp_dir)])

    assert rc == 2
    assert "error:" in capsys.readouterr().err


# --- --version handling ---------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [(None, None), ("latest", None), ("0.2", "== 0.2")],
)
def test_pin_for_version(version, expected):
    result = installer._pin_for_version(version)

    assert result == expected


def test_pin_for_version_warns_when_omitted(capsys):
    installer._pin_for_version(None)

    assert "warning" in capsys.readouterr().err


def test_pin_for_version_latest_no_warning(capsys):
    installer._pin_for_version("latest")

    assert "warning" not in capsys.readouterr().err


def test_installed_version_present():
    result = installer.installed_version()

    assert result == _metadata.version("soliplex-concierge")


def test_installed_version_absent(monkeypatch):
    def _raise(_name):
        raise _metadata.PackageNotFoundError

    monkeypatch.setattr(installer._metadata, "version", _raise)

    assert installer.installed_version() is None


def test_main_version_pins(stack):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--version",
            "0.2",
        ]
    )

    backend = stack / "backend"
    assert rc == 0
    assert (
        '"soliplex-concierge == 0.2"'
        in (backend / "pyproject.toml").read_text()
    )
    assert "soliplex-concierge==0.2" in (backend / "Dockerfile").read_text()


def test_main_version_latest_no_warning(stack, capsys):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--version",
            "latest",
        ]
    )

    out = capsys.readouterr()
    assert rc == 0
    assert "warning" not in out.err
    assert (
        '"soliplex-concierge",'
        in (stack / "backend" / "pyproject.toml").read_text()
    )


def test_main_echoes_installed_version(stack, capsys):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--version",
            "latest",
        ]
    )

    assert rc == 0
    assert installer.installed_version() in capsys.readouterr().out


def _room_rag_stem(stack, room_id="about_concierge-test"):
    cfg = _load(
        stack
        / "backend"
        / "environment"
        / "rooms"
        / room_id
        / "room_config.yaml"
    )
    rag = next(
        s
        for s in cfg["skills"]["skill_configs"]
        if s.get("kind") == installer.RAG_SKILL_KIND
    )
    return rag["rag_lancedb_stem"]


def test_main_rag_stem_defaults_to_haiku(stack):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--version",
            "latest",
        ]
    )

    assert rc == 0
    assert _room_rag_stem(stack) == installer.DEFAULT_RAG_STEM


def test_main_rag_stem_override(stack):
    rc = installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--version",
            "latest",
            "--rag-stem",
            "custom",
        ]
    )

    assert rc == 0
    assert _room_rag_stem(stack) == "custom"


def test_main_echoes_not_installed(stack, capsys, monkeypatch):
    monkeypatch.setattr(installer, "installed_version", lambda: None)

    installer.main(
        [
            "--stack-dir",
            str(stack),
            "--assets-dir",
            str(REPO_ROOT),
            "--version",
            "latest",
        ]
    )

    assert "not installed" in capsys.readouterr().out
