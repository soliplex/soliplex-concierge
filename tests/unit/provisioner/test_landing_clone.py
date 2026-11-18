"""Unit tests for ``soliplex_concierge.provisioner.landing_clone``.

These run real ``git`` against repositories under ``tmp_path``; a local
bare repository stands in for ``origin``. The ``git_config`` fixture gives
each test an empty global configuration of its own (and no system one), so
results do not depend on who runs them.

Each test is laid out in three blank-line-separated phases -- setup, then the
single call under test (the "act"), then the assertions -- and performs that
act exactly once (cases that would repeat it are parametrized or split).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import socket
import subprocess
from unittest import mock

import pytest

from soliplex_concierge.provisioner import landing_clone
from soliplex_concierge.provisioner import timeouts as sc_timeouts

ROOM_ID = "alice:notes"


def _git(path, *args):
    """Read-only git queries for the assertions."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def git_config(tmp_path, monkeypatch):
    """An empty global git config file, in effect for the test."""
    path = tmp_path / "gitconfig"
    path.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def clone(tmp_path, git_config):
    path = tmp_path / "src" / "rooms-provisioned"
    landing_clone.ensure_clone(path)
    return path


@pytest.fixture
def origin(tmp_path, git_config):
    path = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(path)], check=True)
    return path


def _write_room(clone, room_id=ROOM_ID, text="id: x\n"):
    room = clone / room_id
    room.mkdir()
    (room / "room_config.yaml").write_text(text)
    (room / "REQUEST.md").write_text("# x\n")


def _commit(clone, room_id=ROOM_ID):
    return landing_clone.commit_room(
        clone,
        room_id,
        author_name="Alice Example",
        author_email="alice@example.com",
        message=f"Provision room {room_id}",
    )


# ---- locked ----


def test_lock_path(tmp_path):
    path = tmp_path / "src" / "rooms-provisioned"

    result = landing_clone.lock_path(path)

    assert result == tmp_path / "src" / ".rooms-provisioned.lock"


def test_locked_holds_an_exclusive_lock(tmp_path):
    path = tmp_path / "src" / "rooms-provisioned"
    lock = landing_clone.lock_path(path)

    with landing_clone.locked(path):
        with lock.open("a") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    assert not path.exists()  # the lock does not create the clone
    with lock.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released


def _flock_busy(monkeypatch, busy):
    """Patch flock: the first ``busy`` non-blocking tries find it taken."""
    real = fcntl.flock
    tries = []

    def flock(handle, operation):
        if operation & fcntl.LOCK_NB:
            tries.append(operation)
            if len(tries) <= busy:
                raise BlockingIOError
        return real(handle, operation & ~fcntl.LOCK_NB)

    monkeypatch.setattr(landing_clone.fcntl, "flock", mock.Mock(wraps=flock))
    return tries


def _clock(monkeypatch, *times):
    monkeypatch.setattr(
        landing_clone.time, "monotonic", mock.Mock(side_effect=times)
    )
    sleep = mock.Mock()
    monkeypatch.setattr(landing_clone.time, "sleep", sleep)
    return sleep


def _take(path, timeout):
    """Take, then release, the clone's lock: entering is what may raise."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(landing_clone.locked(path, timeout=timeout))


def test_locked_retries_with_backoff(tmp_path, monkeypatch):
    path = tmp_path / "src" / "rooms-provisioned"
    tries = _flock_busy(monkeypatch, busy=3)
    sleep = _clock(monkeypatch, 0, 0, 0, 0)

    with landing_clone.locked(path, timeout=10):
        pass

    assert len(tries) == 4
    assert [call.args[0] for call in sleep.call_args_list] == [0.05, 0.1, 0.2]


def test_locked_gives_up_when_the_clone_stays_busy(tmp_path, monkeypatch):
    path = tmp_path / "src" / "rooms-provisioned"
    landing_clone.lock_path(path).parent.mkdir(parents=True)
    landing_clone.lock_path(path).write_text("other-host pid 7 2026-11-18")
    _flock_busy(monkeypatch, busy=99)
    sleep = _clock(monkeypatch, 0, 0, 9.98, 10)

    with pytest.raises(landing_clone.LandingCloneError) as caught:
        _take(path, timeout=10)

    assert type(caught.value) is landing_clone.CloneBusy
    assert caught.value.timeout == 10
    assert caught.value.holder == "other-host pid 7 2026-11-18"
    assert "(held by other-host pid 7 2026-11-18)" in str(caught.value)
    # the second wait is cut to the 0.02 s left, short of the 0.1 s backoff
    assert [call.args[0] for call in sleep.call_args_list] == pytest.approx(
        [0.05, 0.02]
    )


def test_locked_names_no_holder_it_cannot_read(tmp_path, monkeypatch):
    path = tmp_path / "src" / "rooms-provisioned"
    _flock_busy(monkeypatch, busy=99)
    _clock(monkeypatch, 0, 10)

    with pytest.raises(landing_clone.CloneBusy) as caught:
        _take(path, timeout=10)

    assert caught.value.holder == ""
    assert "held by" not in str(caught.value)


def test_locked_records_its_holder(tmp_path):
    path = tmp_path / "src" / "rooms-provisioned"

    with landing_clone.locked(path):
        holder = landing_clone.lock_path(path).read_text()

    assert holder.startswith(f"{socket.gethostname()} pid {os.getpid()} ")


# ---- ensure_clone ----


def test_ensure_clone_initializes(tmp_path, git_config):
    path = tmp_path / "src" / "rooms-provisioned"

    landing_clone.ensure_clone(path)

    assert _git(path, "rev-parse", "--is-inside-work-tree") == "true"
    assert _git(path, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(path, "remote") == ""


def test_ensure_clone_adds_origin(clone, origin):
    landing_clone.ensure_clone(clone, origin_url=str(origin))

    assert _git(clone, "remote", "get-url", "origin") == str(origin)


def test_ensure_clone_updates_origin(clone, origin, tmp_path):
    landing_clone.ensure_clone(clone, origin_url=str(tmp_path / "old.git"))

    landing_clone.ensure_clone(clone, origin_url=str(origin))

    assert _git(clone, "remote", "get-url", "origin") == str(origin)


def test_ensure_clone_keeps_an_existing_clone(clone):
    _write_room(clone)
    first = _commit(clone)

    landing_clone.ensure_clone(clone)

    assert _git(clone, "rev-parse", "HEAD") == first


# ---- commit_room ----


def test_commit_room(clone):
    _write_room(clone)
    (clone / "stray.txt").write_text("not part of the room\n")

    result = _commit(clone)

    assert result == _git(clone, "rev-parse", "HEAD")
    log = _git(clone, "log", "-1", "--format=%an|%ae|%cn|%ce|%s")
    assert log == (
        "Alice Example|alice@example.com|"
        f"{landing_clone.COMMITTER_NAME}|{landing_clone.COMMITTER_EMAIL}|"
        "Provision room alice:notes"
    )
    committed = _git(clone, "show", "--name-only", "--format=", "HEAD")
    assert committed.splitlines() == [
        "alice:notes/REQUEST.md",
        "alice:notes/room_config.yaml",
    ]
    assert _git(clone, "status", "--porcelain") == "?? stray.txt"


@pytest.mark.parametrize(
    "configured, expected",
    [
        (
            "[user]\n\tname = Ops Bot\n\temail = ops@example.com\n",
            "Ops Bot|ops@example.com",
        ),
        (
            "[user]\n\temail = ops@example.com\n",
            f"{landing_clone.COMMITTER_NAME}|ops@example.com",
        ),
    ],
)
def test_commit_room_committer_from_operator_config(
    clone, git_config, configured, expected
):
    git_config.write_text(configured)
    _write_room(clone)

    _commit(clone)

    log = _git(clone, "log", "-1", "--format=%an|%ae|%cn|%ce")
    assert log == f"Alice Example|alice@example.com|{expected}"


def test_commit_room_runs_operator_hooks(clone, git_config, tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "pre-commit-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git_config.write_text(f"[core]\n\thooksPath = {hooks}\n")
    _write_room(clone)

    _commit(clone)

    assert marker.exists()


def test_commit_room_also_commits_other_paths(clone):
    _write_room(clone)
    fragment = clone / "databases.d" / f"{ROOM_ID}.yaml"
    fragment.parent.mkdir()
    fragment.write_text("fragment\n")
    (clone / "unrelated.txt").write_text("not mine\n")

    landing_clone.commit_room(
        clone,
        ROOM_ID,
        author_name="Alice",
        author_email="alice@example.com",
        message="m",
        also=(f"databases.d/{ROOM_ID}.yaml",),
    )

    committed = _git(clone, "show", "--name-only", "--format=", "HEAD")
    assert f"databases.d/{ROOM_ID}.yaml" in committed.splitlines()
    assert "unrelated.txt" not in committed
    assert _git(clone, "status", "--porcelain") == "?? unrelated.txt"


def test_commit_room_with_nothing_to_commit(clone):
    _write_room(clone)
    _commit(clone)

    with pytest.raises(landing_clone.LandingCloneError) as caught:
        _commit(clone)

    assert type(caught.value) is landing_clone.NothingToCommit
    assert caught.value.room_id == ROOM_ID


def test_commit_room_outside_a_clone(tmp_path, git_config):
    with pytest.raises(landing_clone.LandingCloneError) as caught:
        _commit(tmp_path)

    assert type(caught.value) is landing_clone.GitFailed
    assert caught.value.command[0] == "add"
    assert "not a git repository" in caught.value.stderr


# ---- push ----


def test_git_call_times_out(clone, monkeypatch):
    hang = mock.Mock(side_effect=subprocess.TimeoutExpired(["git"], 1))
    monkeypatch.setattr(landing_clone.sc_timeouts, "run_bounded", hang)

    with pytest.raises(landing_clone.LandingCloneError) as caught:
        landing_clone.ensure_clone(clone, origin_url="x", timeout=1)

    assert type(caught.value) is landing_clone.GitTimedOut
    assert caught.value.command == ("remote",)
    assert caught.value.timeout == 1
    assert "'git remote' timed out after 1 seconds" in str(caught.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda path: landing_clone.ensure_clone(
            path, origin_url="https://g.example/r.git", timeout=7
        ),
        lambda path: (
            _write_room(path),
            landing_clone.commit_room(
                path,
                ROOM_ID,
                author_name="A",
                author_email="a@example.com",
                message="x",
                timeout=7,
            ),
        ),
    ],
    ids=["ensure_clone", "commit_room"],
)
def test_git_calls_pass_their_timeout(clone, monkeypatch, call):
    spy = mock.Mock(wraps=sc_timeouts.run_bounded)
    monkeypatch.setattr(landing_clone.sc_timeouts, "run_bounded", spy)

    call(clone)

    assert {c.kwargs["timeout"] for c in spy.call_args_list} == {7}


def test_push(clone, origin):
    landing_clone.ensure_clone(clone, origin_url=str(origin))
    _write_room(clone)
    head = _commit(clone)

    landing_clone.push(clone)

    assert _git(origin, "rev-parse", "main") == head


def test_push_passes_the_token_through_the_environment(
    clone, origin, monkeypatch
):
    landing_clone.ensure_clone(clone, origin_url=str(origin))
    _write_room(clone)
    _commit(clone)
    spy = mock.Mock(wraps=sc_timeouts.run_bounded)
    monkeypatch.setattr(landing_clone.sc_timeouts, "run_bounded", spy)

    landing_clone.push(clone, token="s3cret")

    [((argv,), kwargs)] = spy.call_args_list
    env = kwargs["env"]
    assert "s3cret" not in " ".join(argv)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: token s3cret"
    assert "s3cret" not in (clone / ".git" / "config").read_text()


def test_push_keeps_config_already_in_the_environment(
    clone, origin, monkeypatch
):
    landing_clone.ensure_clone(clone, origin_url=str(origin))
    _write_room(clone)
    _commit(clone)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "push.followTags")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    spy = mock.Mock(wraps=sc_timeouts.run_bounded)
    monkeypatch.setattr(landing_clone.sc_timeouts, "run_bounded", spy)

    landing_clone.push(clone, token="s3cret")

    [env] = [call.kwargs["env"] for call in spy.call_args_list]
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "push.followTags"
    assert env["GIT_CONFIG_KEY_1"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_1"] == "Authorization: token s3cret"


def test_push_bounds_the_push_with_a_timeout(clone, origin, monkeypatch):
    landing_clone.ensure_clone(clone, origin_url=str(origin))
    _write_room(clone)
    _commit(clone)
    spy = mock.Mock(wraps=sc_timeouts.run_bounded)
    monkeypatch.setattr(landing_clone.sc_timeouts, "run_bounded", spy)

    landing_clone.push(clone)

    assert [call.kwargs["timeout"] for call in spy.call_args_list] == [
        sc_timeouts.PUSH_TIMEOUT
    ]


def test_push_timed_out(clone, monkeypatch):
    hang = mock.Mock(side_effect=subprocess.TimeoutExpired(["git"], 5))
    monkeypatch.setattr(landing_clone.sc_timeouts, "run_bounded", hang)

    with pytest.raises(landing_clone.LandingCloneError) as caught:
        landing_clone.push(clone, timeout=5)

    assert type(caught.value) is landing_clone.PushTimedOut
    assert caught.value.timeout == 5
    assert str(caught.value).endswith("failed: timed out after 5 seconds")


def test_push_without_origin(clone):
    _write_room(clone)
    _commit(clone)

    with pytest.raises(landing_clone.LandingCloneError) as caught:
        landing_clone.push(clone)

    assert type(caught.value) is landing_clone.PushFailed
    assert "origin" in caught.value.stderr


# ---- git availability ----


def test_git_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(landing_clone.shutil, "which", lambda name: None)

    with pytest.raises(landing_clone.LandingCloneError) as caught:
        landing_clone.ensure_clone(tmp_path / "rooms-provisioned")

    assert type(caught.value) is landing_clone.GitMissing
