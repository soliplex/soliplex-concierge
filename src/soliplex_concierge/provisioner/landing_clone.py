"""The provisioned-rooms clone: the git repository rooms land in.

The provisioner is the clone's only writer. Each provisioned room is one
directory (named for the room id) committed with the requester as author,
then pushed to ``origin`` when one is configured.

Git configuration applies as usual, so an operator can set policy for the
provisioner's commits -- e.g. by pointing ``GIT_CONFIG_GLOBAL`` at a
mounted file: its ``user.name`` / ``user.email`` make the committer (the
provisioner's own identity fills in whichever is unset), and its hooks or
signing settings apply.

Every git call uses literal pathspecs (room ids carry ':') and never prompts
for credentials.

Every call is bounded (see :mod:`timeouts`):

- local ones by ``data:sc_timeouts.GIT_TIMEOUT`` (limiting the
  operator's hooks or signing)

- pushs by ``data:sc_timeouts.PUSH_TIMEOUT``

- waiting for the writer's lock by ``data:sc_timeouts.LOCK_TIMEOUT``.

When any timeout expires, git and whatever it started are killed.

External operations:
- git via :mod:`subprocess`
- the lock via :mod:`fcntl`
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
import pathlib
import shutil
import socket
import subprocess
import time

from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import timeouts as sc_timeouts

BRANCH = "main"
COMMITTER_NAME = "Soliplex provisioner"
COMMITTER_EMAIL = "provisioner@soliplex.invalid"
# Waiting for the lock: the first retry's delay, and the longest.
_LOCK_POLL_FIRST = 0.05
_LOCK_POLL_MAX = 1.0


class LandingCloneError(Exception):
    """A user-facing error (printed without a traceback)."""


class GitMissing(LandingCloneError):
    def __init__(self):
        super().__init__("the provisioner needs 'git', which is not installed")


class GitFailed(LandingCloneError):
    def __init__(self, args, stderr):
        self.command, self.stderr = args, stderr
        super().__init__(f"'git {' '.join(args)}' failed: {stderr}")


class GitTimedOut(LandingCloneError):
    def __init__(self, args, timeout):
        self.command, self.timeout = args, timeout
        super().__init__(
            f"'git {' '.join(args)}' timed out after {timeout} seconds"
        )


class CloneBusy(LandingCloneError, interfaces.StoreBusy):
    def __init__(self, path, timeout, holder):
        self.path, self.timeout, self.holder = path, timeout, holder
        held = f" (held by {holder})" if holder else ""
        super().__init__(
            f"the provisioned-rooms clone at {path} stayed locked{held} "
            f"for {timeout} seconds"
        )


class NothingToCommit(LandingCloneError):
    def __init__(self, room_id):
        self.room_id = room_id
        super().__init__(f"nothing to commit for room {room_id!r}")


class PushFailed(LandingCloneError):
    def __init__(self, stderr):
        self.stderr = stderr
        super().__init__(
            f"pushing the provisioned-rooms clone failed: {stderr}"
        )


class PushTimedOut(PushFailed):
    def __init__(self, timeout):
        self.timeout = timeout
        super().__init__(f"timed out after {timeout} seconds")


def _env(extra_config: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for one git call.

    ``extra_config`` is appended to any ``GIT_CONFIG_KEY_<n>`` /
    ``GIT_CONFIG_VALUE_<n>`` entries the environment already carries.
    """
    env = {
        **os.environ,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if extra_config:
        start = int(env.get("GIT_CONFIG_COUNT") or 0)
        for i, (key, value) in enumerate(extra_config.items(), start):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        env["GIT_CONFIG_COUNT"] = str(start + len(extra_config))
    return env


def _git(
    path,
    *args,
    extra_config=None,
    timeout=sc_timeouts.GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run ``git -C path <args>``

    Raise :class:`GitTimedOut` (killing it, and whatever it started) when it
    runs longer than ``timeout``.
    """
    if shutil.which("git") is None:
        raise GitMissing()

    try:
        return sc_timeouts.run_bounded(
            ["git", "-C", str(path), *args],
            env=_env(extra_config),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise GitTimedOut(args, timeout) from error


def _check(
    path,
    *args,
    extra_config=None,
    timeout=sc_timeouts.GIT_TIMEOUT,
) -> str:
    result = _git(path, *args, extra_config=extra_config, timeout=timeout)

    if result.returncode != 0:
        raise GitFailed(args, result.stderr.strip())

    return result.stdout


def _committer_defaults(path, timeout) -> dict[str, str]:
    """The provisioner's identity for whichever of name / email is unset."""
    defaults = {"user.name": COMMITTER_NAME, "user.email": COMMITTER_EMAIL}
    return {
        key: value
        for key, value in defaults.items()
        if _git(path, "config", "--get", key, timeout=timeout).returncode != 0
    }


def lock_path(path: pathlib.Path) -> pathlib.Path:
    """The lock file serializing writers of the clone at ``path``."""
    return path.parent / f".{path.name}.lock"


@contextlib.contextmanager
def locked(path: pathlib.Path, *, timeout: float = sc_timeouts.LOCK_TIMEOUT):
    """Hold the exclusive writer's lock on the clone at ``path``.

    The lock file sits beside the clone, so it is taken before the clone
    exists and never shows in the clone's working tree.

    Its holder writes who it is (host, pid, and since when) into it.

    Raises :class:`CloneBusy`, naming that holder, when the lock stays taken
    for ``timeout`` seconds.
    """
    lock = lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        _acquire(handle, path, timeout)
        try:
            handle.seek(0)
            handle.truncate()
            since = datetime.datetime.now(datetime.UTC).isoformat()
            handle.write(f"{socket.gethostname()} pid {os.getpid()} {since}")
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _acquire(handle, path: pathlib.Path, timeout: float) -> None:
    """Take ``handle``'s lock, retrying with backoff for ``timeout``."""
    deadline = time.monotonic() + timeout
    delay = _LOCK_POLL_FIRST
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                handle.seek(0)
                raise CloneBusy(path, timeout, handle.read().strip()) from None
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _LOCK_POLL_MAX)
        else:
            return


def ensure_clone(
    path: pathlib.Path,
    *,
    origin_url: str | None = None,
    timeout: float = sc_timeouts.GIT_TIMEOUT,
) -> None:
    """Make ``path`` a git repository on :data:`BRANCH`.

    Initializes it when it is not one yet.

    With ``origin_url``, points ``origin`` at it (adding or updating the
    remote).

    Each git call is bounded by ``timeout`` (see :class:`GitTimedOut`).
    """
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        _check(
            path,
            "init",
            "--quiet",
            "--initial-branch",
            BRANCH,
            timeout=timeout,
        )
    if origin_url is not None:
        remotes = _check(path, "remote", timeout=timeout).split()
        verb = "set-url" if "origin" in remotes else "add"
        _check(path, "remote", verb, "origin", origin_url, timeout=timeout)


def commit_room(
    path: pathlib.Path,
    room_id: str,
    *,
    author_name: str,
    author_email: str,
    message: str,
    also: tuple[str, ...] = (),
    timeout: float = sc_timeouts.GIT_TIMEOUT,
) -> str:
    """Commit the ``room_id`` directory of the clone at ``path``.

    Stages only that directory, and the paths in ``also`` (relative to the
    clone).

    The requester is the author; the committer is the configured
    ``user.name`` / ``user.email``, with the provisioner's identity for
    whichever is unset.

    Returns the new commit's id.

    Raises :class:`NothingToCommit` when those paths hold no change.

    Each git call (the commit, with its hooks and signing, among them)
    is bounded by ``timeout`` (see :class:`GitTimedOut`).
    """
    pathspecs = (room_id, *also)
    _check(path, "add", "--all", "--", *pathspecs, timeout=timeout)
    staged = _git(
        path, "diff", "--cached", "--quiet", "--", *pathspecs, timeout=timeout
    )

    if staged.returncode == 0:
        raise NothingToCommit(room_id)

    _check(
        path,
        "commit",
        "--quiet",
        "--author",
        f"{author_name} <{author_email}>",
        "--message",
        message,
        "--",
        *pathspecs,
        extra_config=_committer_defaults(path, timeout),
        timeout=timeout,
    )
    return _check(path, "rev-parse", "HEAD", timeout=timeout).strip()


def push(
    path: pathlib.Path,
    *,
    branch: str = BRANCH,
    token: str | None = None,
    timeout: float = sc_timeouts.PUSH_TIMEOUT,
) -> None:
    """Push ``branch`` of the clone at ``path`` to ``origin``.

    A Gitea ``token`` travels as an ``http.extraHeader`` set through the
    environment, never on the command line or in the clone's config.

    Raises :class:`PushFailed` when the push fails
    Raises :class:`PushTimedOut` when it takes longer than ``timeout`` seconds.
    """
    extra_config = {}

    if token is not None:
        extra_config["http.extraHeader"] = f"Authorization: token {token}"

    try:
        result = _git(
            path,
            "push",
            "--quiet",
            "origin",
            f"{branch}:{branch}",
            extra_config=extra_config,
            timeout=timeout,
        )
    except GitTimedOut as error:
        raise PushTimedOut(timeout) from error

    if result.returncode != 0:
        raise PushFailed(result.stderr.strip())
