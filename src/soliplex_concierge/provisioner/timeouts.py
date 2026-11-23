"""Bounds for the provisioner toolkit's blocking calls (#144).

A provisioning run holds its store's lock while it calls ``soliplex-cli``,
git and the stack's services, so one call that never returns would stop
every later run. Each blocking call in the toolkit takes a ``timeout``,
defaulting to one of these (seconds):

- :data:`CLI_TIMEOUT`: one ``soliplex-cli`` command
- :data:`GIT_TIMEOUT`: one local git command (``commit`` runs the
  operator's hooks and signing)
- :data:`PUSH_TIMEOUT`: one push to ``origin``
- :data:`LOCK_TIMEOUT`: waiting for the clone's writer's lock: the sum of
  the other three
- :data:`HTTP_TIMEOUT`: each connect, read or write of an HTTP call (not
  the whole call)

A deployment wanting others passes ``timeout=`` at its call sites.

Stdlib only.
"""

from __future__ import annotations

import os
import signal
import subprocess

CLI_TIMEOUT = 120
GIT_TIMEOUT = 60
PUSH_TIMEOUT = 60
LOCK_TIMEOUT = CLI_TIMEOUT + GIT_TIMEOUT + PUSH_TIMEOUT
HTTP_TIMEOUT = 30


def run_bounded(
    args: list[str],
    *,
    timeout: float | None,
    input_: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``args`` to completion, as :func:`subprocess.run` would, with
    text output captured; ``timeout`` None waits indefinitely.

    The command runs in its own process group, so that when ``timeout``
    expires the whole group is killed (a wrapper's children, such as a
    ``docker compose run`` container's client, or git's hooks), not just
    the command itself. Raises :class:`subprocess.TimeoutExpired` then.
    """
    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_ is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        shell=False,
    ) as process:
        try:
            stdout, stderr = process.communicate(input_, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            raise
    return subprocess.CompletedProcess(
        args, process.returncode, stdout, stderr
    )


def _kill_group(process: subprocess.Popen) -> None:
    """Kill ``process``'s whole group, and reap ``process``.

    Waits for the process itself, not for its pipes to close: a member
    that left the group could hold them open indefinitely.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:  # it exited meanwhile
        pass
    process.wait()
