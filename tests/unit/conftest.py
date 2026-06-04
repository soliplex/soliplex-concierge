import pathlib
import tempfile
from unittest import mock

import pytest
from soliplex.config import installation as config_installation


@pytest.fixture(scope="module")
def anyio_backend():
    """Run anyio-marked tests on asyncio only (no trio)."""
    return "asyncio"


@pytest.fixture
def temp_dir() -> pathlib.Path:
    with tempfile.TemporaryDirectory() as td:
        yield pathlib.Path(td).resolve()


@pytest.fixture
def installation_config():
    return mock.create_autospec(config_installation.InstallationConfig)
