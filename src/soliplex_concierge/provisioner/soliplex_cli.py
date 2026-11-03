"""Run ``soliplex-cli`` for a provisioner: authorization, checks, audits.

Each function takes ``cli``, the command to run (``soliplex-cli``, or a
deployment's own CLI offering the same commands), and ``environment``, the
installation's path as that command sees it.
"""

import json
import subprocess

# The ACL permissions a room's owner is granted.
OWNER_PERMISSIONS = ("READ", "WRITE")


class CliFailed(Exception):
    """The command could not run, or exited with an unexpected code."""

    def __init__(self, cli, command, output):
        self.cli, self.command, self.output = cli, tuple(command), output
        super().__init__(f"'{cli} {' '.join(command)}' failed:\n{output}")


def run(
    cli: str,
    *command: str,
    stdin: str | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> str:
    """Run ``cli <command>``; return its standard output.

    Raises :class:`CliFailed` when it cannot run, or exits with a code not
    in ``ok_codes``.
    """
    try:
        result = subprocess.run(
            [cli, *command],
            input=stdin,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise CliFailed(cli, command, str(error)) from error
    if result.returncode not in ok_codes:
        raise CliFailed(cli, command, (result.stdout + result.stderr).strip())
    return result.stdout


class NoVerdict(CliFailed):
    """A check gave no verdict (for instance, over a stale ACL entry)."""


def _verdict(cli, command, key, token) -> bool:
    output = run(cli, *command, stdin=token, ok_codes=(0, 1))
    try:
        verdict = json.loads(output)[key]
    except (json.JSONDecodeError, TypeError, KeyError) as error:
        raise NoVerdict(cli, command, output.strip()) from error
    return verdict is True


def is_admin(cli: str, environment: str, token: str) -> bool:
    """Is the holder of the service token ``token`` an admin?

    Raises :class:`NoVerdict` when the check gives none.
    """
    command = ("admin-users", "--quiet", "check", environment)
    return _verdict(cli, command, "is_admin", token)


def may(
    cli: str, environment: str, token: str, room_id: str, permission: str
) -> bool:
    """Does the holder of ``token`` hold ``permission`` on ``room_id``?

    ``permission`` is ``read`` or ``write``; ``room_id`` may be a reserved
    id such as ``__provision__``. Raises :class:`NoVerdict` when the check
    gives none.
    """
    command = (
        "room-authz",
        "--quiet",
        "check",
        environment,
        room_id,
        "--permission",
        permission,
    )
    return _verdict(cli, command, "allowed", token)


def stale_policy_ids(cli: str, environment: str) -> set[str]:
    """The ids of authz policies for rooms the installation does not have.

    ``audit room-authz`` reports them as "stale", exiting 1 with a JSON
    error report (``--quiet``); a clean audit prints nothing. Raises
    :class:`CliFailed` when the audit fails, or its report is unreadable.
    """
    command = ("audit", "--quiet", "room-authz", environment)
    output = run(cli, *command, ok_codes=(0, 1))
    if not output.strip():
        return set()
    try:
        report = json.loads(output).get("room_authz", {})
    except (json.JSONDecodeError, AttributeError) as error:
        raise CliFailed(cli, command, output.strip()) from error
    if "unreachable" in report:
        raise CliFailed(cli, command, report["unreachable"])
    return set(report.get("stale_rooms", ()))


def owner_policy(room_id: str, json_path: str) -> str:
    """A room's authz policy, as JSON: private, its owner granted access.

    ``json_path`` is the owner's access rule (see
    :class:`soliplex_concierge.provisioner.room_ids.RoomOwner`); it is
    granted each of :data:`OWNER_PERMISSIONS`.
    """
    return json.dumps(
        {
            "room_id": room_id,
            "default_allow_deny": "DENY",
            "acl_entries": [
                {
                    "allow_deny": "ALLOW",
                    "permission": permission,
                    "json_path": json_path,
                }
                for permission in OWNER_PERMISSIONS
            ],
        }
    )


def set_policy(cli: str, environment: str, room_id: str, policy: str) -> None:
    """Store ``policy`` (JSON, as :func:`owner_policy` makes) for the room.

    Raises :class:`CliFailed` when the command fails.
    """
    run(cli, "room-authz", "from-yaml", environment, room_id, stdin=policy)


def remove_policy(cli: str, environment: str, room_id: str) -> None:
    """Remove the room's authz policy, leaving it as if never set."""
    set_policy(cli, environment, room_id, "null")


def audit_rooms(cli: str, environment: str) -> None:
    """Audit the installation's rooms; raise :class:`CliFailed` on errors."""
    run(cli, "audit", "--quiet", "rooms", environment)
