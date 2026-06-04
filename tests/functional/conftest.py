"""Fixtures for functional tests that exercise a real Gitea server.

The 'gitea_server' fixture starts a throwaway Gitea container, provisions an
admin user, an access token, and an empty repository, then yields the
connection details. It skips (rather than fails) when Docker is unavailable.
"""

import secrets
import shutil
import subprocess
import time

import httpx
import pytest

GITEA_IMAGE = "gitea/gitea:1.22"
CONTAINER_NAME = "soliplex-concierge-gitea-test"
ADMIN_USER = "concierge"
ADMIN_EMAIL = "concierge@example.com"
REPO_NAME = "about-soliplex"


def _make_admin_password():
    # Random per-run, no literal secret in source. The fixed suffix
    # guarantees the upper/lower/digit/symbol classes some Gitea password
    # policies require.
    return f"{secrets.token_urlsafe(16)}Aa1!"


@pytest.fixture(scope="module")
def anyio_backend():
    """Run anyio-marked tests on asyncio only (no trio)."""
    return "asyncio"


def _docker(*args, check=True, timeout=120):
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _require_docker():
    if shutil.which("docker") is None:
        pytest.skip("docker executable not found")
    try:
        _docker("info")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker daemon not usable: {exc}")


def _wait_for_gitea(base_url, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/api/v1/version", timeout=2)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    pytest.fail("gitea did not become ready in time")


@pytest.fixture(scope="module")
def gitea_server():
    _require_docker()

    # Remove any container left behind by an interrupted prior run.
    _docker("rm", "-f", CONTAINER_NAME, check=False)

    try:
        _docker(
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "-e",
            "GITEA__security__INSTALL_LOCK=true",
            "-p",
            "127.0.0.1:0:3000",
            GITEA_IMAGE,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        pytest.skip(f"could not start gitea container: {exc.stderr}")

    try:
        admin_password = _make_admin_password()
        port_line = (
            _docker("port", CONTAINER_NAME, "3000")
            .stdout.strip()
            .splitlines()[0]
        )
        host_port = port_line.rsplit(":", 1)[1]
        base_url = f"http://127.0.0.1:{host_port}"

        _wait_for_gitea(base_url)

        _docker(
            "exec",
            "-u",
            "git",
            CONTAINER_NAME,
            "gitea",
            "admin",
            "user",
            "create",
            "--username",
            ADMIN_USER,
            "--password",
            admin_password,
            "--email",
            ADMIN_EMAIL,
            "--admin",
            "--must-change-password=false",
        )

        token_resp = httpx.post(
            f"{base_url}/api/v1/users/{ADMIN_USER}/tokens",
            auth=(ADMIN_USER, admin_password),
            json={
                "name": "concierge-test",
                "scopes": ["write:repository", "write:issue"],
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["sha1"]

        # Provision the repo with admin basic auth (full rights); the scoped
        # token is reserved for the tool under test (issue creation).
        repo_resp = httpx.post(
            f"{base_url}/api/v1/user/repos",
            auth=(ADMIN_USER, admin_password),
            json={"name": REPO_NAME, "auto_init": True, "private": False},
            timeout=10,
        )
        repo_resp.raise_for_status()

        yield {
            "host": base_url,
            "owner": ADMIN_USER,
            "repo": REPO_NAME,
            "token": token,
        }
    finally:
        _docker("rm", "-f", CONTAINER_NAME, check=False)
