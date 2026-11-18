"""Functional tests of the provisioner toolkit's timeouts (#144).

These run real processes and wait on real time: a command that hangs, and
the children it starts; a git hook that hangs; a lock another holder keeps
or releases. Run them explicitly:

    uv run py.test --no-cov tests/functional/test_timeouts_functional.py
"""

import fcntl
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from soliplex_concierge.provisioner import landing_clone
from soliplex_concierge.provisioner import soliplex_cli
from soliplex_concierge.provisioner import timeouts

ROOM_ID = "alice--notes"


def _alive(pid):
    """Whether ``pid`` runs (a zombie, awaiting its reaper, does not)."""
    try:
        status = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return status.rsplit(")", 1)[1].split()[0] != "Z"


def _gone(pid, within=5):
    deadline = time.monotonic() + within
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def _hanging_script(path, pid_file):
    """A script that starts a child, records its pid, and hangs."""
    path.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, time\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def git_config(tmp_path, monkeypatch):
    """An empty global git config file, in effect for the test."""
    path = tmp_path / "gitconfig"
    path.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return path


@pytest.fixture
def clone(tmp_path, git_config):
    path = tmp_path / "src" / "rooms-provisioned"
    landing_clone.ensure_clone(path)
    return path


# ---- killing what a timed-out command started ----


def test_run_bounded_kills_the_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = _hanging_script(tmp_path / "hang", pid_file)

    with pytest.raises(subprocess.TimeoutExpired):
        timeouts.run_bounded([script], timeout=1)

    assert _gone(int(pid_file.read_text()))


def test_cli_times_out_and_leaves_no_child(tmp_path):
    pid_file = tmp_path / "child.pid"
    cli = _hanging_script(tmp_path / "soliplex-cli", pid_file)

    with pytest.raises(soliplex_cli.CliTimedOut) as caught:
        soliplex_cli.is_admin(cli, "/environment", "t0ken", timeout=1)

    assert caught.value.timeout == 1
    assert _gone(int(pid_file.read_text()))


def test_commit_times_out_on_a_hanging_hook(tmp_path, clone, git_config):
    pid_file = tmp_path / "child.pid"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    _hanging_script(hooks / "pre-commit", pid_file)
    git_config.write_text(f"[core]\n\thooksPath = {hooks}\n")
    room = clone / ROOM_ID
    room.mkdir()
    (room / "room_config.yaml").write_text("id: x\n")

    with landing_clone.locked(clone):
        with pytest.raises(landing_clone.GitTimedOut) as caught:
            landing_clone.commit_room(
                clone,
                ROOM_ID,
                author_name="Alice Example",
                author_email="alice@example.com",
                message="x",
                timeout=1,
            )

    assert caught.value.command[0] == "commit"
    assert _gone(int(pid_file.read_text()))
    with landing_clone.locked(clone, timeout=0):
        pass  # the lock was released


# ---- waiting for the clone's lock ----


def test_locked_waits_for_the_holder(tmp_path):
    path = tmp_path / "src" / "rooms-provisioned"
    lock = landing_clone.lock_path(path)
    lock.parent.mkdir(parents=True)
    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        release = threading.Timer(0.3, fcntl.flock, (holder, fcntl.LOCK_UN))
        release.start()

        with landing_clone.locked(path, timeout=5):
            pass

        release.join()


def test_locked_gives_up_on_a_busy_clone(tmp_path):
    path = tmp_path / "src" / "rooms-provisioned"

    with landing_clone.locked(path):
        started = time.monotonic()
        with pytest.raises(landing_clone.CloneBusy) as caught:
            with landing_clone.locked(path, timeout=0.5):
                pass

    assert time.monotonic() - started >= 0.5
    assert f"pid {os.getpid()}" in caught.value.holder
