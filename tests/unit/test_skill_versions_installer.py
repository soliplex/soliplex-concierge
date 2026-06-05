import importlib.util
import pathlib

from soliplex_concierge import installer

REPO_ROOT = pathlib.Path(installer.__file__).resolve().parents[2]
SKILL = "soliplex-concierge-installer"


def _load():
    path = REPO_ROOT / "skills" / SKILL / "scripts" / "skill_versions.py"
    spec = importlib.util.spec_from_file_location(f"sv_{SKILL}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_versions_constants():
    module = _load()

    assert module.OWNER == "soliplex"
    assert module.REPO == "soliplex-concierge"
    assert module.ASSET_TARBALL == "soliplex-concierge-installer-skill.tar.gz"
    assert module.POINTER_TAG == "installer-skill-latest"
    assert module._ROLLING_RE.match("installer-skill-2026.06.05-abc1234")
