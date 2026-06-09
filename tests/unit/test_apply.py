import contextlib
import hashlib
import importlib.util
import pathlib
import shutil
import sys
import tarfile

import pytest

# apply.py is a bundled skill script (not an installed package), so load it by
# path like the other skill-script tests. Its only third-party import
# (ruamel.yaml) is provided by the dev dependency group. It is registered in
# sys.modules before execution so its dataclasses resolve their own module.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_INSTALLER_SKILL = REPO_ROOT / "skills" / "soliplex-concierge-installer"
_APPLY = _INSTALLER_SKILL / "scripts" / "apply.py"
ASSETS = _INSTALLER_SKILL / "assets"
ROOM_SKILL = REPO_ROOT / "skills" / "soliplex-concierge-room"

_spec = importlib.util.spec_from_file_location("concierge_apply", _APPLY)
apply = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = apply
_spec.loader.exec_module(apply)

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


@pytest.fixture
def docs_skill(tmp_path) -> pathlib.Path:
    """A minimal local 'soliplex-docs' skill dir for offline installs."""
    root = tmp_path / "soliplex-docs"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: soliplex-docs\n---\n")
    return root


def _load(path: pathlib.Path):
    return apply._yaml().load(path.read_text())


# --- resolve_stack --------------------------------------------------------


def test_resolve_stack_ok(stack):
    result = apply.resolve_stack(str(stack))

    assert result == stack


@pytest.mark.parametrize("marker", apply.STACK_MARKERS)
def test_resolve_stack_missing_marker(stack, marker):
    (stack / marker).unlink()

    with pytest.raises(apply.InstallerError, match=marker):
        apply.resolve_stack(str(stack))


# --- resolve_assets -------------------------------------------------------


def test_resolve_assets_default_is_bundle():
    result = apply.resolve_assets()

    assert result == ASSETS


def test_resolve_assets_missing(temp_dir, monkeypatch):
    monkeypatch.setattr(apply, "ASSETS", temp_dir)

    with pytest.raises(apply.InstallerError, match="assets"):
        apply.resolve_assets()


# --- resolve_published_skill / download -----------------------------------

_SPECS = [apply.ROOM, apply.DOCS]
_SPEC_IDS = ["room", "docs"]


def _make_skill_tarball(tmp_path, spec, *, with_skill_md=True):
    """Build a '<name>/...'-rooted .tar.gz like a published skill build."""
    root = tmp_path / "src" / spec.name
    root.mkdir(parents=True)
    if with_skill_md:
        (root / "SKILL.md").write_text("---\nname: x\n---\n")
    else:
        (root / "README.md").write_text("no skill here\n")
    tarball = tmp_path / spec.asset_tarball
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(root, arcname=spec.name)
    return tarball


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_resolve_published_skill_override(tmp_path, spec):
    (tmp_path / "SKILL.md").write_text("x")
    ctx = contextlib.ExitStack()

    result = apply.resolve_published_skill(
        spec, str(tmp_path), None, _opts(), tmp_path, ctx
    )

    assert result == tmp_path.resolve()
    ctx.close()


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_resolve_published_skill_override_missing(temp_dir, spec):
    with pytest.raises(apply.InstallerError, match=spec.dir_flag):
        apply.resolve_published_skill(
            spec,
            str(temp_dir),
            None,
            _opts(),
            temp_dir,
            contextlib.ExitStack(),
        )


def test_resolve_published_skill_dry_run_skips(stack):
    result = apply.resolve_published_skill(
        apply.ROOM,
        None,
        None,
        _opts(dry_run=True),
        stack,
        contextlib.ExitStack(),
    )

    assert result is None


def test_resolve_published_skill_already_installed_skips(stack):
    dst = stack / "backend" / "environment" / "skills" / apply.DOCS.name
    dst.mkdir(parents=True)

    result = apply.resolve_published_skill(
        apply.DOCS, None, None, _opts(), stack, contextlib.ExitStack()
    )

    assert result is None


# --- published-skill download helpers -------------------------------------


def test_get_rejects_unsupported_scheme():
    with pytest.raises(apply.InstallerError, match="could not download"):
        apply._get("ftp://example.com/x", apply.ROOM)


def test_get_reads_file_url_with_token(tmp_path, monkeypatch):
    blob = tmp_path / "x.json"
    blob.write_text("{}")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    result = apply._get(blob.as_uri(), apply.ROOM)

    assert result == b"{}"


def test_get_reads_file_url_without_token(tmp_path, monkeypatch):
    blob = tmp_path / "x.json"
    blob.write_text("{}")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    result = apply._get(blob.as_uri(), apply.ROOM)

    assert result == b"{}"


def test_get_http_error(monkeypatch):
    def _raise(req):
        raise apply.urllib_error.HTTPError(req.full_url, 404, "NF", {}, None)

    monkeypatch.setattr(apply.urllib_request, "urlopen", _raise)

    with pytest.raises(apply.InstallerError, match="HTTP 404"):
        apply._get("https://example.com/x", apply.ROOM)


def test_get_url_error(monkeypatch):
    def _raise(req):
        raise apply.urllib_error.URLError("boom")

    monkeypatch.setattr(apply.urllib_request, "urlopen", _raise)

    with pytest.raises(apply.InstallerError, match="boom"):
        apply._get("https://example.com/x", apply.ROOM)


def test_read_pointer_ok(monkeypatch):
    payload = b'{"tag": "t", "asset_url": "u"}'
    monkeypatch.setattr(apply, "_get", lambda url, spec, **kw: payload)

    result = apply._read_pointer(apply.ROOM)

    assert result["tag"] == "t"


def test_read_pointer_bad_manifest(monkeypatch):
    monkeypatch.setattr(apply, "_get", lambda url, spec, **kw: b"not json")

    with pytest.raises(apply.InstallerError, match="invalid manifest"):
        apply._read_pointer(apply.ROOM)


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_resolve_target_explicit(spec):
    tag, url, sha256 = apply._resolve_target(spec, "v0.4")

    assert tag == "v0.4"
    assert url.endswith(f"/v0.4/{spec.asset_tarball}")
    assert sha256 is None


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_download_skill_explicit_version(tmp_path, monkeypatch, spec):
    tarball = _make_skill_tarball(tmp_path, spec)
    monkeypatch.setattr(
        apply, "_get", lambda url, s, **kw: tarball.read_bytes()
    )
    dest = tmp_path / "dl"
    dest.mkdir()

    result = apply.download_skill(spec, "v0.4", dest)

    assert (result / "SKILL.md").is_file()


def test_download_skill_checksum_mismatch(tmp_path, monkeypatch):
    tarball = _make_skill_tarball(tmp_path, apply.ROOM)
    monkeypatch.setattr(
        apply,
        "_read_pointer",
        lambda spec: {
            "tag": "t",
            "asset_url": tarball.as_uri(),
            "sha256": "dead",
        },
    )
    dest = tmp_path / "dl"
    dest.mkdir()

    with pytest.raises(apply.InstallerError, match="checksum mismatch"):
        apply.download_skill(apply.ROOM, None, dest)


def test_download_skill_no_skill_md(tmp_path, monkeypatch):
    tarball = _make_skill_tarball(tmp_path, apply.DOCS, with_skill_md=False)
    monkeypatch.setattr(
        apply, "_get", lambda url, spec, **kw: tarball.read_bytes()
    )
    dest = tmp_path / "dl"
    dest.mkdir()

    with pytest.raises(apply.InstallerError, match="no SKILL.md"):
        apply.download_skill(apply.DOCS, "v0.4", dest)


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

    result = apply.compose_project_name(temp_dir)

    assert result == (expected if expected is not None else temp_dir.name)


def test_default_room_id(stack):
    result = apply.default_room_id(stack)

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
    new_text, action = apply.add_pyproject_dep(
        _PYPROJECT, pin, with_truststore
    )

    assert action == apply.ADDED
    assert expected in new_text


def test_add_pyproject_dep_unchanged():
    text = _PYPROJECT.replace(
        '    "soliplex",\n', '    "soliplex",\n    "soliplex-concierge",\n'
    )

    new_text, action = apply.add_pyproject_dep(text)

    assert action == apply.UNCHANGED
    assert new_text == text


def test_add_pyproject_dep_truststore_unchanged_when_bare_present():
    # Known limitation: re-running with the extra is a no-op once the bare
    # name is already present (the idempotency probe matches the bare name).
    text = _PYPROJECT.replace(
        '    "soliplex",\n', '    "soliplex",\n    "soliplex-concierge",\n'
    )

    new_text, action = apply.add_pyproject_dep(text, with_truststore=True)

    assert action == apply.UNCHANGED
    assert new_text == text


def test_add_pyproject_dep_empty_array_indent():
    text = "[project]\ndependencies = [\n]\n"

    new_text, action = apply.add_pyproject_dep(text)

    assert action == apply.ADDED
    assert '    "soliplex-concierge",\n' in new_text


def test_add_pyproject_dep_bad():
    text = '[project]\ndependencies = ["soliplex"]\n'

    with pytest.raises(apply.InstallerError, match="dependencies"):
        apply.add_pyproject_dep(text)


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
    new_text, action = apply.add_dockerfile_dep(
        _DOCKERFILE, pin, with_truststore
    )

    assert action == apply.ADDED
    assert expected in new_text


def test_add_dockerfile_dep_unchanged():
    text = _DOCKERFILE.replace(
        "      soliplex \\\n",
        "      soliplex \\\n      soliplex-concierge \\\n",
    )

    new_text, action = apply.add_dockerfile_dep(text)

    assert action == apply.UNCHANGED
    assert new_text == text


def test_add_dockerfile_dep_truststore_unchanged_when_bare_present():
    # Same known limitation as the pyproject case: the bare name already in
    # the 'uv add' block short-circuits before the extra can be added.
    text = _DOCKERFILE.replace(
        "      soliplex \\\n",
        "      soliplex \\\n      soliplex-concierge \\\n",
    )

    new_text, action = apply.add_dockerfile_dep(text, with_truststore=True)

    assert action == apply.UNCHANGED
    assert new_text == text


def test_add_dockerfile_dep_bad():
    with pytest.raises(apply.InstallerError, match="Dockerfile"):
        apply.add_dockerfile_dep("RUN echo no uv add here\n")


# --- update_env -----------------------------------------------------------


@pytest.mark.parametrize("base", ["X=1\n", "X=1"])
def test_update_env_added(base):
    new_text, action = apply.update_env(base, "https://g", "tok")

    assert action == apply.ADDED
    assert "GITEA_HOST=https://g" in new_text
    assert "GITEA_ACCESS_TOKEN=tok" in new_text


def test_update_env_unchanged():
    text = "GITEA_HOST=already\n"

    new_text, action = apply.update_env(text, "https://g", "tok")

    assert action == apply.UNCHANGED
    assert new_text == text


# --- merge_installation ---------------------------------------------------


def _loads(text):
    return apply._yaml().load(text)


def test_merge_installation_adds():
    new_text, results = apply.merge_installation(
        _INSTALLATION, "about_concierge-test"
    )

    assert set(results.values()) == {apply.ADDED}
    data = _loads(new_text)
    assert apply.TOOL_CONFIG in data["meta"]["tool_configs"]
    assert apply.GITEA_HOST in data["environment"]
    assert any(
        s.get("secret_name") == apply.GITEA_TOKEN_SECRET
        for s in data["secrets"]
    )
    assert any(
        s.get("skill_name") == apply.SKILL_NAME for s in data["skill_configs"]
    )
    assert any(
        s.get("skill_name") == apply.DOCS.name for s in data["skill_configs"]
    )
    assert "./rooms/about_concierge-test" in data["room_paths"]


def test_merge_installation_leaves_other_sections_verbatim():
    new_text, _ = apply.merge_installation(_INSTALLATION, "about_x")

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
    once, _ = apply.merge_installation(_INSTALLATION, "about_x")

    twice, results = apply.merge_installation(once, "about_x")

    assert set(results.values()) == {apply.UNCHANGED}
    assert twice == once


def test_add_meta_tool_config_creates_when_meta_ends_file():
    lines = ["meta:\n", "  # only a comment\n"]

    action = apply._add_meta_tool_config(lines)

    assert action == apply.ADDED
    assert "  tool_configs:\n" in lines


def test_merge_installation_appends_to_existing_tool_configs():
    new_text, results = apply.merge_installation(
        _INSTALLATION_META_TC, "about_x"
    )

    tcs = _loads(new_text)["meta"]["tool_configs"]
    assert results["installation: meta.tool_configs"] == apply.ADDED
    assert apply.TOOL_CONFIG in tcs
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

    with pytest.raises(apply.InstallerError, match=section):
        apply.merge_installation(text, "about_x")


# --- _patch_room_config ---------------------------------------------------


@pytest.mark.parametrize(
    "owner,repo",
    [(None, None), ("acme", "reqs"), ("acme", None), (None, "reqs")],
)
def test_patch_room_config(temp_dir, owner, repo):
    src = ASSETS / "rooms" / apply.ASSET_ROOM
    room = temp_dir / "room"
    shutil.copytree(src, room)
    cfg = room / "room_config.yaml"
    opts = _opts(room_id="about_acme", owner=owner, repo=repo)

    apply._patch_room_config(cfg, opts)

    data = _load(cfg)
    assert data["id"] == "about_acme"
    tool = next(
        t for t in data["tools"] if t.get("tool_name") == apply.GITEA_TOOL
    )
    assert tool["owner"] == (
        owner if owner is not None else "your-gitea-owner"
    )
    assert tool["repo"] == (repo if repo is not None else "soliplex-requests")


# --- install_room / install_skill -----------------------------------------


def _opts(**kw):
    kw.setdefault("room_id", "about_concierge-test")
    return apply.Options(**kw)


def test_install_room_added(stack):
    action = apply.install_room(ASSETS, stack, _opts())

    rooms = stack / "backend" / "environment" / "rooms"
    cfg = _load(rooms / "about_concierge-test" / "room_config.yaml")
    assert action == apply.ADDED
    assert cfg["id"] == "about_concierge-test"


def test_install_room_unchanged(stack):
    rooms = stack / "backend" / "environment" / "rooms"
    (rooms / "about_concierge-test").mkdir()

    action = apply.install_room(ASSETS, stack, _opts())

    assert action == apply.UNCHANGED


def test_install_room_dry_run(stack):
    action = apply.install_room(ASSETS, stack, _opts(dry_run=True))

    rooms = stack / "backend" / "environment" / "rooms"
    assert action == apply.ADDED
    assert not (rooms / "about_concierge-test").exists()


def test_install_room_force(stack):
    rooms = stack / "backend" / "environment" / "rooms"
    (rooms / "about_concierge-test").mkdir()

    action = apply.install_room(ASSETS, stack, _opts(force=True))

    assert action == apply.ADDED
    assert (rooms / "about_concierge-test" / "room_config.yaml").is_file()


def test_install_skill_added(stack):
    action = apply.install_skill(apply.SKILL_NAME, ROOM_SKILL, stack, _opts())

    skill_dir = stack / "backend" / "environment" / "skills" / apply.SKILL_NAME
    assert action == apply.ADDED
    assert (skill_dir / "SKILL.md").is_file()
    # the whole tree is copied, including the request templates
    assert (skill_dir / "assets" / "room_creation_request.md").is_file()
    assert (skill_dir / "assets" / "room_access_request.md").is_file()


def test_install_skill_unchanged(stack):
    skill_dir = stack / "backend" / "environment" / "skills" / apply.SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old")

    action = apply.install_skill(apply.SKILL_NAME, ROOM_SKILL, stack, _opts())

    assert action == apply.UNCHANGED


def test_install_skill_dry_run(stack):
    action = apply.install_skill(
        apply.SKILL_NAME, ROOM_SKILL, stack, _opts(dry_run=True)
    )

    skill = (
        stack
        / "backend"
        / "environment"
        / "skills"
        / apply.SKILL_NAME
        / "SKILL.md"
    )
    assert action == apply.ADDED
    assert not skill.exists()


def test_install_skill_force(stack):
    skill_dir = stack / "backend" / "environment" / "skills" / apply.SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old")

    action = apply.install_skill(
        apply.SKILL_NAME, ROOM_SKILL, stack, _opts(force=True)
    )

    assert action == apply.ADDED
    assert (skill_dir / "SKILL.md").read_text() != "old"


# --- main / apply end-to-end ----------------------------------------------


def test_main_applies(stack, docs_skill, capsys):
    rc = apply.main(
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
    assert apply.GITEA_HOST in inst["environment"]
    assert (
        backend
        / "environment"
        / "rooms"
        / "about_concierge-test"
        / "room_config.yaml"
    ).is_file()
    skills = backend / "environment" / "skills"
    assert (skills / apply.SKILL_NAME / "SKILL.md").is_file()
    assert (skills / apply.DOCS.name / "SKILL.md").is_file()
    assert "GITEA_HOST=" in (stack / ".env").read_text()
    assert "edit owner/repo" in capsys.readouterr().out


def test_main_dry_run(stack, capsys):
    rc = apply.main(["--stack-dir", str(stack), "--dry-run"])

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
    apply.apply(stack, ASSETS, ROOM_SKILL, docs_skill, opts)

    results = apply.apply(stack, ASSETS, ROOM_SKILL, docs_skill, opts)

    assert set(results.values()) == {apply.UNCHANGED}


def test_main_room_id_override(stack, docs_skill):
    rc = apply.main(
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
    rc = apply.main(
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
        t for t in cfg["tools"] if t.get("tool_name") == apply.GITEA_TOOL
    )
    assert (tool["owner"], tool["repo"]) == ("acme", "reqs")
    assert "edit owner/repo" not in capsys.readouterr().out


def test_main_with_truststore(stack, docs_skill):
    rc = apply.main(
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


def test_main_not_a_stack(temp_dir, capsys):
    rc = apply.main(["--stack-dir", str(temp_dir)])

    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_main_downloads_skills(stack, tmp_path, monkeypatch):
    room_tb = _make_skill_tarball(tmp_path / "room", apply.ROOM)
    docs_tb = _make_skill_tarball(tmp_path / "docs", apply.DOCS)
    tarballs = {apply.ROOM.name: room_tb, apply.DOCS.name: docs_tb}

    def _pointer(spec):
        tarball = tarballs[spec.name]
        return {
            "tag": f"{spec.name}-x",
            "asset_url": tarball.as_uri(),
            "sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(apply, "_read_pointer", _pointer)

    rc = apply.main(["--stack-dir", str(stack), "--owner", "o", "--repo", "r"])

    assert rc == 0
    skills = stack / "backend" / "environment" / "skills"
    assert (skills / apply.ROOM.name / "SKILL.md").is_file()
    assert (skills / apply.DOCS.name / "SKILL.md").is_file()


def test_main_skill_download_fails(stack, capsys, monkeypatch):
    def _boom(url, spec, **kw):
        raise apply.InstallerError.skill_bad_scheme(spec, url)

    monkeypatch.setattr(apply, "_get", _boom)

    rc = apply.main(["--stack-dir", str(stack)])

    assert rc == 2
    # the room skill is resolved first, so its --room-skill-dir hint shows.
    assert "--room-skill-dir" in capsys.readouterr().err


# --- --version handling ---------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [(None, None), ("latest", None), ("0.2", "== 0.2")],
)
def test_pin_for_version(version, expected):
    result = apply._pin_for_version(version)

    assert result == expected


def test_pin_for_version_warns_when_omitted(capsys):
    apply._pin_for_version(None)

    assert "warning" in capsys.readouterr().err


def test_pin_for_version_latest_no_warning(capsys):
    apply._pin_for_version("latest")

    assert "warning" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "owner,repo",
    [(None, None), ("acme", None), (None, "reqs")],
)
def test_warn_missing_owner_repo_warns(capsys, owner, repo):
    apply._warn_missing_owner_repo(owner, repo)

    assert "warning" in capsys.readouterr().err


def test_warn_missing_owner_repo_silent_when_both_set(capsys):
    apply._warn_missing_owner_repo("acme", "reqs")

    assert "warning" not in capsys.readouterr().err


def test_main_version_pins(stack, docs_skill):
    rc = apply.main(
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
    rc = apply.main(
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
