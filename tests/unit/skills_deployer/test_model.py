import pytest

from soliplex_concierge.skills_deployer.model import Version


def test_version_copies_blob_digests(tmp_path):
    digests = {"a": "original"}
    version = Version("team", "revision", tmp_path, (), digests)

    digests["a"] = "changed"
    digests["b"] = "added"

    assert version.blob_digests == {"a": "original"}


def test_version_rejects_blob_digest_mutation(tmp_path):
    version = Version("team", "revision", tmp_path, (), {"a": "original"})

    with pytest.raises(TypeError):
        version.blob_digests["a"] = "changed"

    assert version.blob_digests == {"a": "original"}
