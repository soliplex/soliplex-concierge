import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL = "soliplex-concierge-admin"


def _load():
    path = REPO_ROOT / "skills" / SKILL / "scripts" / "skill_versions.py"
    spec = importlib.util.spec_from_file_location(f"sv_{SKILL}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_versions_spec():
    module = _load()
    spec = module.SPEC

    assert spec.skill_name == "soliplex-concierge-admin"
    assert spec.owner == "soliplex"
    assert spec.repo == "soliplex-concierge"
    assert spec.asset_tarball == "soliplex-concierge-admin-skill.tar.gz"
    assert spec.pointer_tag == "admin-skill-latest"
    assert spec.compare_scope == "tree"
    assert spec.rolling_re.match("admin-skill-2026.06.05-abc1234")
