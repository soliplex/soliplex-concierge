"""Unit tests for ``soliplex_concierge.provisioner.soliplex_cli``.

``fake_cli`` stands in for the CLI; the tests of the commands built on
``run`` replace it, recording what they would run.
"""

import json
import subprocess
import sys

import pytest

from soliplex_concierge.provisioner import soliplex_cli

ENVIRONMENT = "/environment"
ROOM = "alice:q3-plans"


@pytest.fixture
def fake_cli(tmp_path):
    """A stand-in CLI: echoes its args and stdin, exits $FAKE_RC.

    If ``$FAKE_STDERR`` is set, it also prints it to stderr first.
    """
    script = tmp_path / "soliplex-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "stderr = os.environ.get('FAKE_STDERR')\n"
        "if stderr:\n"
        "    print(stderr, file=sys.stderr)\n"
        "print(' '.join(sys.argv[1:]), sys.stdin.read())\n"
        "sys.exit(int(os.environ.get('FAKE_RC', '0')))\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def recorded(monkeypatch):
    """Replace ``_run``; record each call, answering ``recorded.output``.

    ``recorded.stderr`` fills the fake process's stderr.
    """

    class Recorded:
        output = ""
        stderr = ""
        calls = []

    def _run(cli, command, stdin, ok_codes):
        Recorded.calls.append((cli, command, stdin, ok_codes))
        return subprocess.CompletedProcess(
            [cli, *command],
            returncode=0,
            stdout=Recorded.output,
            stderr=Recorded.stderr,
        )

    monkeypatch.setattr(soliplex_cli, "_run", _run)
    Recorded.calls = []
    return Recorded


# ---- run ----


def test_run(fake_cli):
    result = soliplex_cli.run(fake_cli, "audit", "rooms", stdin="null")

    assert result == "audit rooms null\n"


def test_run_accepts_listed_exit_codes(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "1")

    result = soliplex_cli.run(fake_cli, "audit", ok_codes=(0, 1))

    assert result == "audit \n"


def test_run_failed(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "1")

    with pytest.raises(soliplex_cli.CliFailed) as caught:
        soliplex_cli.run(fake_cli, "audit", "rooms")

    assert caught.value.cli == fake_cli
    assert caught.value.command == ("audit", "rooms")
    assert caught.value.output == "audit rooms"
    assert str(caught.value) == (
        f"'{fake_cli} audit rooms' failed:\naudit rooms"
    )


def test_run_missing(tmp_path):
    cli = str(tmp_path / "no-such-cli")

    with pytest.raises(soliplex_cli.CliFailed) as caught:
        soliplex_cli.run(cli, "audit", "rooms")

    assert "No such file or directory" in caught.value.output


# ---- stale_policy_ids ----


@pytest.mark.parametrize(
    "output, expected",
    [
        ("", set()),
        ("\n", set()),
        (
            json.dumps(
                {"room_authz": {"stale_rooms": [ROOM, "__provision__"]}}
            ),
            {ROOM, "__provision__"},
        ),
        (
            json.dumps({"room_authz": {"invalid_acls": {"x": [["$[", "e"]]}}}),
            set(),
        ),
        (json.dumps({"other_section": {}}), set()),
    ],
)
def test_stale_policy_ids(recorded, output, expected):
    recorded.output = output

    result = soliplex_cli.stale_policy_ids("soliplex-cli", ENVIRONMENT)

    assert result == expected
    assert recorded.calls == [
        (
            "soliplex-cli",
            ("audit", "--quiet", "room-authz", ENVIRONMENT),
            None,
            (0, 1),
        )
    ]


@pytest.mark.parametrize(
    "output, detail",
    [
        (
            json.dumps({"room_authz": {"unreachable": "connection refused"}}),
            "connection refused",
        ),
        ("not json", "not json"),
        ("[1, 2]", "[1, 2]"),
    ],
)
def test_stale_policy_ids_fails(recorded, output, detail):
    recorded.output = output

    with pytest.raises(soliplex_cli.CliFailed) as caught:
        soliplex_cli.stale_policy_ids("soliplex-cli", ENVIRONMENT)

    assert caught.value.output == detail


# ---- policies ----


def test_owner_policy():
    json_path = '$[?$.email == "al\\"ice@example.com"]'

    result = soliplex_cli.owner_policy("al_ice_example.com:x", json_path)

    assert json.loads(result) == {
        "room_id": "al_ice_example.com:x",
        "default_allow_deny": "DENY",
        "acl_entries": [
            {
                "allow_deny": "ALLOW",
                "permission": "READ",
                "json_path": json_path,
            },
            {
                "allow_deny": "ALLOW",
                "permission": "WRITE",
                "json_path": json_path,
            },
        ],
    }


def test_set_policy(recorded):
    policy = soliplex_cli.owner_policy(ROOM, "$[?'staff' in @.groups]")

    result = soliplex_cli.set_policy("soliplex-cli", ENVIRONMENT, ROOM, policy)

    assert result is None
    assert recorded.calls == [
        (
            "soliplex-cli",
            ("room-authz", "from-yaml", ENVIRONMENT, ROOM),
            policy,
            (0,),
        )
    ]


def test_remove_policy(recorded):
    result = soliplex_cli.remove_policy("soliplex-cli", ENVIRONMENT, ROOM)

    assert result is None
    [(_cli, command, stdin, _ok)] = recorded.calls
    assert command == ("room-authz", "from-yaml", ENVIRONMENT, ROOM)
    assert stdin == "null"


def test_audit_rooms(recorded):
    result = soliplex_cli.audit_rooms("ragserver-cli", ENVIRONMENT)

    assert result is None
    assert recorded.calls == [
        (
            "ragserver-cli",
            ("audit", "--quiet", "rooms", ENVIRONMENT),
            None,
            (0,),
        )
    ]


# ---- checks ----


@pytest.mark.parametrize(
    "output, expected",
    [
        ('{"is_admin": true}', True),
        ('{"is_admin": false}', False),
        ('{"is_admin": "yes"}', False),
    ],
)
def test_is_admin(recorded, output, expected):
    recorded.output = output

    result = soliplex_cli.is_admin("soliplex-cli", ENVIRONMENT, "t0ken")

    assert result is expected
    assert recorded.calls == [
        (
            "soliplex-cli",
            ("admin-users", "--quiet", "check", ENVIRONMENT),
            "t0ken",
            (0, 1),
        )
    ]


@pytest.mark.parametrize(
    "output, expected",
    [('{"allowed": true}', True), ('{"allowed": false}', False)],
)
def test_may(recorded, output, expected):
    recorded.output = output

    result = soliplex_cli.may(
        "soliplex-cli", ENVIRONMENT, "t0ken", "__provision__", "write"
    )

    assert result is expected
    assert recorded.calls == [
        (
            "soliplex-cli",
            (
                "room-authz",
                "--quiet",
                "check",
                ENVIRONMENT,
                "__provision__",
                "--permission",
                "write",
            ),
            "t0ken",
            (0, 1),
        )
    ]


@pytest.mark.parametrize("output", ["", "not json", "[1]", '{"other": 1}'])
def test_is_admin_without_a_verdict(recorded, output):
    recorded.output = output

    with pytest.raises(soliplex_cli.NoVerdict) as caught:
        soliplex_cli.is_admin("soliplex-cli", ENVIRONMENT, "t0ken")

    assert isinstance(caught.value, soliplex_cli.CliFailed)
    assert caught.value.output == output.strip()


def test_check_crash_reports_stderr(fake_cli, monkeypatch):
    """A crashed check surfaces its stderr in the NoVerdict message."""
    monkeypatch.setenv("FAKE_RC", "1")
    monkeypatch.setenv("FAKE_STDERR", "MissingEnvVar: GITEA_HOST")

    with pytest.raises(soliplex_cli.NoVerdict) as caught:
        soliplex_cli.is_admin(fake_cli, ENVIRONMENT, "t0ken")

    assert caught.value.output == (
        "admin-users --quiet check /environment t0ken\n"
        "MissingEnvVar: GITEA_HOST"
    )
