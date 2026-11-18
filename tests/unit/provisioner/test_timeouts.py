"""Unit tests for ``soliplex_concierge.provisioner.timeouts``."""

import os
import signal
import subprocess
import sys
from unittest import mock

import pytest

from soliplex_concierge.provisioner import timeouts


def test_lock_timeout_is_the_sum_of_the_others():
    result = timeouts.LOCK_TIMEOUT

    assert result == (
        timeouts.CLI_TIMEOUT + timeouts.GIT_TIMEOUT + timeouts.PUSH_TIMEOUT
    )


@pytest.mark.parametrize("timeout", [None, 5])
def test_run_bounded(timeout):
    script = (
        "import os, sys; "
        "print(sys.stdin.read().upper(), os.environ['WHO']); "
        "print('warned', file=sys.stderr); "
        "sys.exit(3)"
    )

    result = timeouts.run_bounded(
        [sys.executable, "-c", script],
        timeout=timeout,
        input_="hello",
        env={**os.environ, "WHO": "me"},
    )

    assert result.returncode == 3
    assert result.stdout == "HELLO me\n"
    assert result.stderr == "warned\n"


def _popen(monkeypatch):
    """Patch Popen with a process whose 'communicate' times out."""
    process = mock.MagicMock(pid=4242)
    process.__enter__.return_value = process
    process.communicate.side_effect = subprocess.TimeoutExpired(["x"], 1)
    monkeypatch.setattr(
        timeouts.subprocess, "Popen", mock.Mock(return_value=process)
    )
    return process


@pytest.mark.parametrize(
    "killpg_error", [None, ProcessLookupError()], ids=["killed", "exited"]
)
def test_run_bounded_kills_the_group_on_timeout(monkeypatch, killpg_error):
    process = _popen(monkeypatch)
    killpg = mock.Mock(side_effect=killpg_error)
    monkeypatch.setattr(timeouts.os, "killpg", killpg)

    with pytest.raises(subprocess.TimeoutExpired):
        timeouts.run_bounded(["x"], timeout=1)

    killpg.assert_called_once_with(4242, signal.SIGKILL)
    process.wait.assert_called_once_with()
    assert timeouts.subprocess.Popen.call_args.kwargs["start_new_session"]
