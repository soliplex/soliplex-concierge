import contextlib
import importlib.util
import pathlib
import shutil

import pytest

from soliplex_concierge import installer

# The installer logic lives in the package (imported above); the skill ships a
# thin 'install_concierge.py' shim over it, exercised by
# test_install_concierge_shim_delegates_to_library below.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_INSTALLER_SKILL = REPO_ROOT / "skills" / "soliplex-concierge-installer"
_SCRIPT = _INSTALLER_SKILL / "scripts" / "install_concierge.py"
ASSETS = _INSTALLER_SKILL / "assets"
ROOM_SKILL = REPO_ROOT / "skills" / "soliplex-concierge-room"

_COMPOSE = "name: concierge-test\nservices: {}\n"

# A stack built by soliplex-template with 'include_gitea': a 'gitea' service
# (backed by postgres) that 'scripts/init_gitea.py' provisions.
_COMPOSE_LOCAL_GITEA = """\
name: concierge-test
services:
  postgres:
    image: postgres:16
  gitea:
    image: gitea/gitea:1.22
"""

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

room_paths:
  - "./rooms/chat"
"""

# Variant with an explicit *filesystem* skill_configs whitelist active: adding
# the concierge skills would narrow it, so the install aborts unless confirmed.
_INSTALLATION_FS_WHITELIST = """\
meta:
  # nothing registered yet

environment:
  - "OLLAMA_BASE_URL"

secrets:
  - secret_name: "URL_SAFE_TOKEN_SECRET"

skill_configs:
  - skill_name: "bare-bones"
    kind: "filesystem"

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


@pytest.fixture
def docs_skill(tmp_path) -> pathlib.Path:
    """A minimal local 'soliplex-docs' skill dir for offline installs."""
    root = tmp_path / "soliplex-docs"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: soliplex-docs\n---\n")
    return root


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


# --- has_local_gitea ------------------------------------------------------


@pytest.mark.parametrize(
    "compose, expected",
    [
        # a service literally named 'gitea'
        ("services:\n  gitea:\n    image: gitea/gitea:1.22\n", True),
        # matched by image even under a different service name
        ("services:\n  vcs:\n    image: gitea/gitea:1.22\n", True),
        # matched by name even with no 'image:' key
        ("services:\n  gitea:\n    build: ./gitea\n", True),
        # a non-gitea service with a non-mapping spec
        ("services:\n  weird:\n", False),
        # other services, no gitea
        ("services:\n  postgres:\n    image: postgres:16\n", False),
        # an empty services mapping, and no services at all
        ("name: x\nservices: {}\n", False),
        ("name: x\n", False),
    ],
)
def test_has_local_gitea(temp_dir, compose, expected):
    (temp_dir / "docker-compose.yml").write_text(compose)

    result = installer.has_local_gitea(temp_dir)

    assert result is expected


# --- resolve_assets -------------------------------------------------------


def test_resolve_assets_ok():
    result = installer.resolve_assets(ASSETS)

    assert result == ASSETS


def test_resolve_assets_missing(temp_dir):
    with pytest.raises(installer.InstallerError, match="assets"):
        installer.resolve_assets(temp_dir)


# --- resolve_published_skill / download -----------------------------------

_SPECS = [installer.ROOM, installer.DOCS]
_SPEC_IDS = ["room", "docs"]


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_resolve_published_skill_override(tmp_path, spec):
    (tmp_path / "SKILL.md").write_text("x")
    ctx = contextlib.ExitStack()

    result = installer.resolve_published_skill(
        spec, str(tmp_path), None, _opts(), tmp_path, ctx
    )

    assert result == tmp_path.resolve()
    ctx.close()


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_resolve_published_skill_override_missing(temp_dir, spec):
    with pytest.raises(installer.InstallerError, match=spec.dir_flag):
        installer.resolve_published_skill(
            spec,
            str(temp_dir),
            None,
            _opts(),
            temp_dir,
            contextlib.ExitStack(),
        )


def test_resolve_published_skill_dry_run_skips(stack):
    result = installer.resolve_published_skill(
        installer.ROOM,
        None,
        None,
        _opts(dry_run=True),
        stack,
        contextlib.ExitStack(),
    )

    assert result is None


def test_resolve_published_skill_already_installed_skips(stack):
    dst = stack / "backend" / "environment" / "skills" / installer.DOCS.name
    dst.mkdir(parents=True)

    result = installer.resolve_published_skill(
        installer.DOCS, None, None, _opts(), stack, contextlib.ExitStack()
    )

    assert result is None


# --- resolve_published_skill download delegation --------------------------


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_published_property_adapts_to_library_spec(spec):
    published = spec.published

    assert isinstance(published, installer.install.PublishedSkill)
    assert published.name == spec.name
    assert published.owner == spec.owner
    assert published.repo == spec.repo
    assert published.asset_tarball == spec.asset_tarball
    assert published.pointer_tag == spec.pointer_tag
    assert published.pointer_manifest == spec.pointer_manifest


def test_resolve_published_skill_downloads_via_library(
    stack, tmp_path, monkeypatch
):
    root = tmp_path / installer.ROOM.name
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: x\n---\n")
    calls = []

    def _fake(published, version, dest):
        calls.append((published.name, version))
        return root

    monkeypatch.setattr(installer.install, "download_skill", _fake)

    result = installer.resolve_published_skill(
        installer.ROOM, None, "v0.4", _opts(), stack, contextlib.ExitStack()
    )

    assert result == root
    assert calls == [(installer.ROOM.name, "v0.4")]


def test_resolve_published_skill_pointer_unavailable(stack, monkeypatch):
    def _boom(published, version, dest):
        raise installer.install.PointerUnavailable(published.pointer_tag)

    monkeypatch.setattr(installer.install, "download_skill", _boom)

    with pytest.raises(
        installer.InstallerError, match="room-skill-latest.*pointer"
    ):
        installer.resolve_published_skill(
            installer.ROOM, None, None, _opts(), stack, contextlib.ExitStack()
        )


def test_resolve_published_skill_download_error_hint(stack, monkeypatch):
    def _boom(published, version, dest):
        raise installer.install.releases.UnsupportedURLScheme("ftp://x", "ftp")

    monkeypatch.setattr(installer.install, "download_skill", _boom)

    with pytest.raises(installer.InstallerError, match="--docs-skill-dir"):
        installer.resolve_published_skill(
            installer.DOCS, None, "v1", _opts(), stack, contextlib.ExitStack()
        )


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
    "pin,with_truststore,expected",
    [
        (None, False, '"soliplex-concierge",'),
        ("== 0.2", False, '"soliplex-concierge == 0.2",'),
        (None, True, '"soliplex-concierge[truststore]",'),
        ("== 0.2", True, '"soliplex-concierge[truststore] == 0.2",'),
    ],
)
def test_add_pyproject_dep_added(pin, with_truststore, expected):
    new_text, action = installer.add_pyproject_dep(
        _PYPROJECT, pin, with_truststore
    )

    assert action == installer.ADDED
    assert expected in new_text


def test_add_pyproject_dep_unchanged():
    text = _PYPROJECT.replace(
        '    "soliplex",\n', '    "soliplex",\n    "soliplex-concierge",\n'
    )

    new_text, action = installer.add_pyproject_dep(text)

    assert action == installer.UNCHANGED
    assert new_text == text


def test_add_pyproject_dep_truststore_unchanged_when_bare_present():
    # Known limitation: re-running with the extra is a no-op once the bare
    # name is already present (the idempotency probe matches the bare name).
    text = _PYPROJECT.replace(
        '    "soliplex",\n', '    "soliplex",\n    "soliplex-concierge",\n'
    )

    new_text, action = installer.add_pyproject_dep(text, with_truststore=True)

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
    "pin,with_truststore,expected",
    [
        (None, False, "soliplex-concierge \\"),
        ("== 0.2", False, "soliplex-concierge==0.2 \\"),
        (None, True, "soliplex-concierge[truststore] \\"),
        ("== 0.2", True, "soliplex-concierge[truststore]==0.2 \\"),
    ],
)
def test_add_dockerfile_dep_added(pin, with_truststore, expected):
    new_text, action = installer.add_dockerfile_dep(
        _DOCKERFILE, pin, with_truststore
    )

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


def test_add_dockerfile_dep_truststore_unchanged_when_bare_present():
    # Same known limitation as the pyproject case: the bare name already in
    # the 'uv add' block short-circuits before the extra can be added.
    text = _DOCKERFILE.replace(
        "      soliplex \\\n",
        "      soliplex \\\n      soliplex-concierge \\\n",
    )

    new_text, action = installer.add_dockerfile_dep(text, with_truststore=True)

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
    new_text, results = installer.merge_installation(_INSTALLATION)

    assert results["installation: meta.tool_configs"] == installer.ADDED
    assert results["installation: environment"] == installer.ADDED
    assert results["installation: secrets"] == installer.ADDED
    # A permissive stack auto-discovers the skills (installed under ./skills),
    # so the whitelist entries are COVERED -- nothing is added.
    assert results["installation: skill_configs"] == installer.COVERED
    assert results["installation: skill_configs (docs)"] == installer.COVERED
    data = _loads(new_text)
    assert installer.TOOL_CONFIG in data["meta"]["tool_configs"]
    assert installer.GITEA_HOST in data["environment"]
    assert any(
        s.get("secret_name") == installer.GITEA_TOKEN_SECRET
        for s in data["secrets"]
    )
    # room_paths is wired by the room install, not merge_installation.
    assert "installation: room_paths" not in results
    assert "skill_configs" not in data  # permissive: section left absent


def test_merge_installation_leaves_other_sections_verbatim():
    new_text, _ = installer.merge_installation(_INSTALLATION)

    # Unrelated sections are byte-identical -- no reindent, no reflow.
    assert '  - id: "default_chat"\n    model_name: "gemma4:26b"' in new_text
    assert (
        "sandbox_config:\n"
        "    environments_path: /sandbox/environments\n"
        "    workdirs_path: /sandbox/workdirs" in new_text
    )
    assert '  - "OLLAMA_BASE_URL"' in new_text  # pre-existing item preserved


def test_merge_installation_idempotent():
    once, _ = installer.merge_installation(_INSTALLATION)

    twice, results = installer.merge_installation(once)

    assert set(results.values()) == {installer.UNCHANGED, installer.COVERED}
    assert twice == once


def test_merge_installation_appends_to_existing_tool_configs():
    new_text, results = installer.merge_installation(_INSTALLATION_META_TC)

    tcs = _loads(new_text)["meta"]["tool_configs"]
    assert results["installation: meta.tool_configs"] == installer.ADDED
    assert installer.TOOL_CONFIG in tcs
    assert "some.Existing.ToolConfig" in tcs


def test_merge_installation_aborts_on_active_skill_whitelist():
    with pytest.raises(
        installer.InstallerError, match="confirm-skill-whitelist"
    ):
        installer.merge_installation(_INSTALLATION_FS_WHITELIST)


def test_merge_installation_confirm_adds_to_active_whitelist():
    new_text, results = installer.merge_installation(
        _INSTALLATION_FS_WHITELIST, confirm_skill_whitelist=True
    )

    assert results["installation: skill_configs"] == installer.ADDED
    names = [s.get("skill_name") for s in _loads(new_text)["skill_configs"]]
    assert installer.SKILL_NAME in names
    assert installer.DOCS.name in names
    assert "bare-bones" in names  # the operator's entry is preserved


# --- _patch_room_config ---------------------------------------------------


@pytest.mark.parametrize(
    "owner,repo",
    [(None, None), ("acme", "reqs"), ("acme", None), (None, "reqs")],
)
def test_patch_room_config(temp_dir, owner, repo):
    src = ASSETS / "rooms" / installer.ASSET_ROOM
    room = temp_dir / "room"
    shutil.copytree(src, room)
    cfg = room / "room_config.yaml"
    opts = _opts(room_id="about_acme", owner=owner, repo=repo)

    installer._patch_room_config(cfg, opts)

    data = _load(cfg)
    assert data["id"] == "about_acme"
    tool = next(
        t for t in data["tools"] if t.get("tool_name") == installer.GITEA_TOOL
    )
    assert tool["owner"] == (
        owner if owner is not None else "your-gitea-owner"
    )
    assert tool["repo"] == (repo if repo is not None else "soliplex-requests")


# --- install_room / install_skill -----------------------------------------


def _opts(**kw):
    kw.setdefault("room_id", "about_concierge-test")
    return installer.Options(**kw)


def _main(argv):
    """Run main with the assets dir as the leading positional (shim-style)."""
    return installer.main([str(ASSETS), *argv])


def _skills(stack):
    return stack / "backend" / "environment" / "skills"


def test_install_room_added(stack):
    room_action, room_paths_action = installer.install_room(
        ASSETS, stack, _opts()
    )

    rooms = stack / "backend" / "environment" / "rooms"
    cfg = _load(rooms / "about_concierge-test" / "room_config.yaml")
    assert room_action == installer.ADDED
    assert cfg["id"] == "about_concierge-test"
    # the enumerated room_paths gains the new room (via install_room_from)
    assert room_paths_action == installer.ADDED
    inst = _load(stack / "backend" / "environment" / "installation.yaml")
    assert "./rooms/about_concierge-test" in inst["room_paths"]


def test_install_room_unchanged(stack):
    rooms = stack / "backend" / "environment" / "rooms"
    (rooms / "about_concierge-test").mkdir()

    room_action, _ = installer.install_room(ASSETS, stack, _opts())

    assert room_action == installer.UNCHANGED


def test_install_room_dry_run(stack):
    room_action, _ = installer.install_room(ASSETS, stack, _opts(dry_run=True))

    rooms = stack / "backend" / "environment" / "rooms"
    assert room_action == installer.ADDED
    assert not (rooms / "about_concierge-test").exists()


def test_install_room_force(stack):
    rooms = stack / "backend" / "environment" / "rooms"
    (rooms / "about_concierge-test").mkdir()

    room_action, _ = installer.install_room(ASSETS, stack, _opts(force=True))

    assert room_action == installer.ADDED
    assert (rooms / "about_concierge-test" / "room_config.yaml").is_file()


def test_install_skill_added(stack):
    action = installer.install_skill(
        installer.SKILL_NAME, ROOM_SKILL, stack, _opts()
    )

    skill_dir = _skills(stack) / installer.SKILL_NAME
    assert action == installer.ADDED
    assert (skill_dir / "SKILL.md").is_file()
    # the whole tree is copied, including the request templates
    assert (skill_dir / "assets" / "room_creation_request.md").is_file()
    assert (skill_dir / "assets" / "room_access_request.md").is_file()


def test_install_skill_room_defangs_version_management(stack):
    skill_dir = _skills(stack) / installer.SKILL_NAME

    action = installer.install_skill(
        installer.SKILL_NAME, ROOM_SKILL, stack, _opts()
    )

    assert action == installer.ADDED
    assert not (skill_dir / "scripts" / "skill_versions.py").exists()
    skill_md = (skill_dir / "SKILL.md").read_text()
    # the section's heading survives, but its helper invocations and the
    # source-only admonition are replaced by the library defang note
    assert "## Managing this skill's version" in skill_md
    assert "uv run scripts/skill_versions.py" not in skill_md
    assert "installed copies differ" not in skill_md
    # the library note names the installer skill but gives no re-run path
    assert "`soliplex-concierge-installer` skill" in skill_md
    assert "from inside the room" in skill_md
    assert "re-run the installer" not in skill_md


def test_install_skill_defang_bounds_to_one_section(stack, tmp_path):
    # the docs skill uses a different heading and keeps a 'Documentation map'
    # section after the self-management one, which must survive the rewrite
    src = tmp_path / "soliplex-docs"
    (src / "scripts" / "__pycache__").mkdir(parents=True)
    (src / "scripts" / "skill_versions.py").write_text("# helper\n")
    (src / "scripts" / "__pycache__" / "skill_versions.pyc").write_text("x")
    (src / "SKILL.md").write_text(
        "# Soliplex documentation\n\n"
        "## Checking for updates\n\n"
        "Run `uv run scripts/skill_versions.py upgrade` to update.\n\n"
        "## Documentation map\n\n"
        "- a topic\n"
    )

    action = installer.install_skill(installer.DOCS.name, src, stack, _opts())

    skill_dir = _skills(stack) / installer.DOCS.name
    skill_md = (skill_dir / "SKILL.md").read_text()
    assert action == installer.ADDED
    # the helper, its bytecode cache, and the now-empty scripts/ are all gone
    assert not (skill_dir / "scripts").exists()
    assert "## Checking for updates" in skill_md  # this skill's own heading
    assert "uv run scripts/skill_versions.py" not in skill_md
    assert "`soliplex-concierge-installer` skill" in skill_md
    assert "## Documentation map" in skill_md  # later section preserved
    assert "- a topic" in skill_md


def test_install_skill_without_helper_is_untouched(stack, tmp_path):
    # no section references the helper, so nothing is rewritten and an
    # unrelated script in scripts/ is left in place
    src = tmp_path / "soliplex-docs"
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "other.py").write_text("x\n")
    (src / "SKILL.md").write_text("# Title\n\n## How to use\n\nstuff.\n")

    action = installer.install_skill(installer.DOCS.name, src, stack, _opts())

    skill_dir = _skills(stack) / installer.DOCS.name
    assert action == installer.ADDED
    assert (skill_dir / "scripts" / "other.py").is_file()
    assert (skill_dir / "SKILL.md").read_text().count("## How to use") == 1


def test_install_skill_unchanged(stack):
    skill_dir = _skills(stack) / installer.SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old")

    action = installer.install_skill(
        installer.SKILL_NAME, ROOM_SKILL, stack, _opts()
    )

    assert action == installer.UNCHANGED


def test_install_skill_dry_run(stack):
    action = installer.install_skill(
        installer.SKILL_NAME, ROOM_SKILL, stack, _opts(dry_run=True)
    )

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
    skill_dir = _skills(stack) / installer.SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old")

    action = installer.install_skill(
        installer.SKILL_NAME, ROOM_SKILL, stack, _opts(force=True)
    )

    assert action == installer.ADDED
    assert (skill_dir / "SKILL.md").read_text() != "old"


# --- stack gitea_issues.py shim -------------------------------------------


@pytest.mark.parametrize(
    "pin,with_truststore,expected",
    [
        (None, False, '"soliplex-concierge>=0.6"'),
        ("== 0.2", False, '"soliplex-concierge == 0.2"'),
        (None, True, '"soliplex-concierge[truststore]>=0.6"'),
        ("== 0.2", True, '"soliplex-concierge[truststore] == 0.2"'),
    ],
)
def test_stack_gitea_script_dependency(pin, with_truststore, expected):
    text = installer.stack_gitea_script(pin, with_truststore)

    assert expected in text
    assert text.startswith("#!/usr/bin/env -S uv run --script")
    assert "from soliplex_concierge.gitea_admin import main" in text


def test_install_gitea_script_added(stack):
    action = installer.install_gitea_script(stack, _opts())

    script = stack / "scripts" / "gitea_issues.py"
    assert action == installer.ADDED
    assert "soliplex_concierge.gitea_admin" in script.read_text()


def test_install_gitea_script_unchanged(stack):
    script = stack / "scripts" / "gitea_issues.py"
    script.parent.mkdir()
    script.write_text("old")

    action = installer.install_gitea_script(stack, _opts())

    assert action == installer.UNCHANGED
    assert script.read_text() == "old"


def test_install_gitea_script_dry_run(stack):
    action = installer.install_gitea_script(stack, _opts(dry_run=True))

    assert action == installer.ADDED
    assert not (stack / "scripts" / "gitea_issues.py").exists()


def test_install_gitea_script_force(stack):
    script = stack / "scripts" / "gitea_issues.py"
    script.parent.mkdir()
    script.write_text("old")

    action = installer.install_gitea_script(stack, _opts(force=True))

    assert action == installer.ADDED
    assert script.read_text() != "old"


# --- main / apply end-to-end ----------------------------------------------


def test_main_applies(stack, docs_skill, capsys):
    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
        ]
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
    skills = backend / "environment" / "skills"
    assert (skills / installer.SKILL_NAME / "SKILL.md").is_file()
    assert (skills / installer.DOCS.name / "SKILL.md").is_file()
    assert "GITEA_HOST=" in (stack / ".env").read_text()
    assert "edit owner/repo" in capsys.readouterr().out


def test_main_dry_run(stack, capsys):
    rc = _main(["--stack-dir", str(stack), "--dry-run"])

    assert rc == 0
    backend = stack / "backend"
    assert (backend / "pyproject.toml").read_text() == _PYPROJECT
    assert (stack / ".env").read_text() == _ENV
    assert not (
        backend / "environment" / "rooms" / "about_concierge-test"
    ).exists()
    assert "would apply" in capsys.readouterr().out


def test_main_idempotent(stack, docs_skill):
    opts = _opts()
    installer.apply(stack, ASSETS, ROOM_SKILL, docs_skill, opts)

    results = installer.apply(stack, ASSETS, ROOM_SKILL, docs_skill, opts)

    # Re-run: everything already present (UNCHANGED); the permissive
    # skill_configs entries stay COVERED.
    assert set(results.values()) == {installer.UNCHANGED, installer.COVERED}


def test_main_room_id_override(stack, docs_skill):
    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
            "--room-id",
            "custom_room",
        ]
    )

    assert rc == 0
    assert (
        stack / "backend" / "environment" / "rooms" / "custom_room"
    ).is_dir()


def test_main_with_owner_repo(stack, docs_skill, capsys):
    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
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


def _room_tool(stack, room_id="about_concierge-test"):
    cfg = _load(
        stack
        / "backend"
        / "environment"
        / "rooms"
        / room_id
        / "room_config.yaml"
    )
    return next(
        t for t in cfg["tools"] if t.get("tool_name") == installer.GITEA_TOOL
    )


def test_main_local_gitea_defaults(stack, docs_skill, capsys):
    (stack / "docker-compose.yml").write_text(_COMPOSE_LOCAL_GITEA)

    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
        ]
    )

    assert rc == 0
    tool = _room_tool(stack)
    assert (tool["owner"], tool["repo"]) == (
        installer.LOCAL_GITEA_OWNER,
        installer.LOCAL_GITEA_REPO,
    )
    # '.env' is left untouched -- init_gitea.py owns GITEA_HOST/token.
    assert (stack / ".env").read_text() == _ENV
    out = capsys.readouterr().out
    assert "local Gitea detected" in out
    assert "init_gitea.py" in out


def test_main_local_gitea_explicit_owner_repo_wins(stack, docs_skill):
    (stack / "docker-compose.yml").write_text(_COMPOSE_LOCAL_GITEA)

    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
            "--owner",
            "acme",
            "--repo",
            "reqs",
        ]
    )

    assert rc == 0
    tool = _room_tool(stack)
    assert (tool["owner"], tool["repo"]) == ("acme", "reqs")


def test_main_no_local_gitea_keeps_placeholders(stack, docs_skill, capsys):
    (stack / "docker-compose.yml").write_text(_COMPOSE_LOCAL_GITEA)

    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
            "--no-local-gitea",
        ]
    )

    assert rc == 0
    tool = _room_tool(stack)
    assert tool["owner"] == "your-gitea-owner"
    assert "GITEA_HOST=" in (stack / ".env").read_text()
    assert "edit owner/repo" in capsys.readouterr().out


def test_main_with_truststore(stack, docs_skill):
    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
            "--with-truststore",
        ]
    )

    assert rc == 0
    backend = stack / "backend"
    assert (
        "soliplex-concierge[truststore]"
        in (backend / "pyproject.toml").read_text()
    )
    assert (
        "soliplex-concierge[truststore]"
        in (backend / "Dockerfile").read_text()
    )
    assert (
        "soliplex-concierge[truststore]"
        in (stack / "scripts" / "gitea_issues.py").read_text()
    )


def test_main_not_a_stack(temp_dir, capsys):
    rc = _main(["--stack-dir", str(temp_dir)])

    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_main_downloads_skills(stack, monkeypatch):
    # the library download is faked to materialize a minimal skill tree, so
    # main() exercises resolve -> install_skill -> defang for both skills.
    def _fake_download(published, version, dest):
        root = dest / published.name
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: x\n---\n")
        return root

    monkeypatch.setattr(installer.install, "download_skill", _fake_download)

    rc = _main(["--stack-dir", str(stack), "--owner", "o", "--repo", "r"])

    assert rc == 0
    skills = stack / "backend" / "environment" / "skills"
    assert (skills / installer.ROOM.name / "SKILL.md").is_file()
    assert (skills / installer.DOCS.name / "SKILL.md").is_file()


def test_main_skill_download_fails(stack, capsys, monkeypatch):
    def _boom(published, version, dest):
        raise installer.install.releases.GitHubAPIError(
            published.pointer_url(), "HTTP 404"
        )

    monkeypatch.setattr(installer.install, "download_skill", _boom)

    rc = _main(["--stack-dir", str(stack)])

    assert rc == 2
    # the room skill is resolved first, so its --room-skill-dir hint shows.
    assert "--room-skill-dir" in capsys.readouterr().err


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


@pytest.mark.parametrize(
    "owner,repo",
    [(None, None), ("acme", None), (None, "reqs")],
)
def test_warn_missing_owner_repo_warns(capsys, owner, repo):
    installer._warn_missing_owner_repo(owner, repo)

    assert "warning" in capsys.readouterr().err


def test_warn_missing_owner_repo_silent_when_both_set(capsys):
    installer._warn_missing_owner_repo("acme", "reqs")

    assert "warning" not in capsys.readouterr().err


def test_main_version_pins(stack, docs_skill):
    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
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


def test_main_version_latest_no_warning(stack, docs_skill, capsys):
    rc = _main(
        [
            "--stack-dir",
            str(stack),
            "--room-skill-dir",
            str(ROOM_SKILL),
            "--docs-skill-dir",
            str(docs_skill),
            "--version",
            "latest",
            "--owner",
            "acme",
            "--repo",
            "reqs",
        ]
    )

    out = capsys.readouterr()
    assert rc == 0
    assert "warning" not in out.err
    assert (
        '"soliplex-concierge",'
        in (stack / "backend" / "pyproject.toml").read_text()
    )


# --- install_concierge.py shim --------------------------------------------


def test_install_concierge_shim_delegates_to_library():
    spec = importlib.util.spec_from_file_location("install_concierge", _SCRIPT)
    shim = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(shim)

    assert shim.installer is installer
    assert shim._ASSETS == ASSETS
