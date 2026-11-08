"""Read git trees and check the structure of a team's skills."""

import contextlib
import os
import pathlib
import re
import selectors
import subprocess
import time
import unicodedata

from soliplex_concierge.skills_deployer.model import ALLOWED_MODES
from soliplex_concierge.skills_deployer.model import RESERVED_PREFIXES
from soliplex_concierge.skills_deployer.model import RESOURCES_DIRNAME
from soliplex_concierge.skills_deployer.model import SKILL_FILENAME
from soliplex_concierge.skills_deployer.model import SKILL_NAME_MAX
from soliplex_concierge.skills_deployer.model import SKILL_NAME_RE
from soliplex_concierge.skills_deployer.model import TEAM_SLUG_RE
from soliplex_concierge.skills_deployer.model import Check
from soliplex_concierge.skills_deployer.model import Entry
from soliplex_concierge.skills_deployer.model import Limits
from soliplex_concierge.skills_deployer.model import Violation


class _TreeLimitError(Exception):
    """Report a bound reached while reading git output."""


def _records(
    command: list[str],
    max_records: int,
    deadline: float,
    *,
    input: bytes | None = None,
    delimiter: bytes = b"\0",
    count: int = 0,
):
    """Yield bounded records while draining git's stderr."""
    if input is not None and len(input) > 65:
        message = "Git batch input exceeds 65 bytes"
        raise ValueError(message)
    with (
        subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process,
        selectors.DefaultSelector() as selector,
    ):
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        pending = b""
        stderr = bytearray()
        try:
            if input is not None:
                process.stdin.write(input)
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    message = "Git tree read timeout exceeded"
                    raise _TreeLimitError(message)
                for key, _events in selector.select(remaining):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "stderr":
                        if len(stderr) + len(chunk) > 8192:
                            message = "Git stderr exceeds 8192 bytes"
                            raise _TreeLimitError(message)
                        stderr.extend(chunk)
                    else:
                        pending += chunk
                        while delimiter in pending:
                            record, pending = pending.split(delimiter, 1)
                            if len(record) > 8192:
                                message = "Git tree record exceeds 8192 bytes"
                                raise _TreeLimitError(message)
                            count += 1
                            if count > max_records:
                                message = (
                                    "Git tree record count exceeds "
                                    f"{max_records}"
                                )
                                raise _TreeLimitError(message)
                            yield record
                        if len(pending) > 8192:
                            message = "Git tree record exceeds 8192 bytes"
                            raise _TreeLimitError(message)
            if pending:
                message = "Unterminated Git output record"
                raise ValueError(message)
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=max(0, remaining))
            except subprocess.TimeoutExpired as error:
                message = "Git tree read timeout exceeded"
                raise _TreeLimitError(message) from error
            if process.returncode:
                raise ValueError(stderr.decode("utf-8", errors="replace"))
        finally:
            process.kill()
            process.wait()


def _valid_utf8(path: bytes) -> bool:
    """Avoid walking descendants of an undecodable directory."""
    try:
        path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def _walk_tree(
    repo: pathlib.Path,
    revision: str,
    max_records: int,
    max_tree_bytes: int,
    deadline: float,
):
    """Walk git trees after checking each object's uncompressed size."""
    command = ["git", "--no-replace-objects", "-C", str(repo)]
    trees = [(b"", revision)]
    count = 0
    while trees:
        prefix, tree = trees.pop()
        with contextlib.closing(
            _records(
                [
                    *command,
                    "cat-file",
                    "-Z",
                    "--batch-check=%(objecttype) %(objectsize)",
                ],
                1,
                deadline,
                input=tree.encode("ascii") + b"\0",
            )
        ) as records:
            metadata = next(records, b"").split()
        if len(metadata) != 2:
            message = "Missing or malformed Git object metadata"
            raise ValueError(message)
        kind, size = metadata
        if size == b"missing":
            message = f"Unknown revision or object {tree}"
            raise ValueError(message)
        expected = b"commit" if tree == revision else b"tree"
        if kind != expected:
            message = (
                f"Expected {expected.decode('ascii')} object, "
                f"got {kind.decode('ascii')}"
            )
            raise ValueError(message)
        if int(size) > max_tree_bytes:
            message = (
                f"Git {kind.decode('ascii')} size {int(size)} "
                f"exceeds {max_tree_bytes} bytes"
            )
            raise _TreeLimitError(message)
        if kind == b"commit":
            with contextlib.closing(
                _records(
                    [*command, "cat-file", "commit", tree],
                    1,
                    deadline,
                    delimiter=b"\n",
                )
            ) as records:
                header = next(records, b"").split()
            if len(header) != 2 or header[0] != b"tree":
                message = "Missing or malformed Git commit tree header"
                raise ValueError(message)
            tree = header[1].decode("ascii")
            trees.append((prefix, tree))
            continue
        for record in _records(
            [
                *command,
                "ls-tree",
                "-z",
                "-l",
                "--full-tree",
                "--end-of-options",
                tree,
            ],
            max_records,
            deadline,
            count=count,
        ):
            count += 1
            header, raw_path = record.split(b"\t", 1)
            raw_path = prefix + raw_path
            if len(header) + 1 + len(raw_path) > 8192:
                message = "Git tree record exceeds 8192 bytes"
                raise _TreeLimitError(message)
            fields = header.split()
            mode, kind, oid = fields[:3]
            if kind == b"tree" and _valid_utf8(raw_path):
                trees.append((raw_path + b"/", oid.decode("ascii")))
            yield fields, raw_path


def read_tree(
    repo: pathlib.Path,
    revision: str,
    *,
    max_records: int = 10000,
    max_tree_bytes: int = 1024 * 1024,
    timeout: float = 30.0,
) -> tuple[list[Entry], list[Violation]]:
    """Read bounded git entries, preserving empty directories.

    Requires Git with support for ``cat-file -Z``. Commit and tree object
    reads are size-bounded; blob sizes are recorded for dimension checks.
    """
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
        message = "Revision must be a full 40 or 64 character lowercase hex id"
        raise ValueError(message)
    deadline = time.monotonic() + timeout
    entries = []
    directories = []
    parents = set()
    violations = []
    try:
        for fields, raw_path in _walk_tree(
            repo, revision, max_records, max_tree_bytes, deadline
        ):
            mode, kind, oid = fields[:3]
            try:
                path = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                violations.append(
                    Violation(
                        Check.LAYOUT,
                        repr(raw_path),
                        f"Path {raw_path!r} must be valid UTF-8",
                    )
                )
                continue
            parents.add(raw_path.rpartition(b"/")[0])
            if kind == b"tree":
                directories.append(Entry(mode.decode(), oid.decode(), 0, path))
            else:
                size = fields[3]
                entries.append(
                    Entry(
                        mode=mode.decode("ascii"),
                        oid=oid.decode("ascii"),
                        size=0 if size == b"-" else int(size),
                        path=path,
                    )
                )
    except _TreeLimitError as error:
        violations.append(Violation(Check.LIMITS, "", str(error)))
        return [], sorted(violations, key=lambda v: (v.check, v.path))
    entries.extend(
        entry
        for entry in directories
        if entry.path.encode("utf-8") not in parents
    )
    return entries, sorted(violations, key=lambda v: (v.check, v.path))


def _normalized(value: str) -> str:
    """Normalize names for collision checks."""
    return unicodedata.normalize(
        "NFD", unicodedata.normalize("NFD", value).casefold()
    )


def _check_layout(
    path: str, parts: list[str], *, is_directory: bool
) -> list[Violation]:
    """Check the components and placement of one entry."""
    is_skill_md = (
        len(parts) == 2 and parts[1] == SKILL_FILENAME and not is_directory
    )
    is_resource = len(parts) >= 3 and parts[1] == RESOURCES_DIRNAME
    rules = (
        (
            "" in parts or ".." in parts,
            "Use relative paths without empty components or '..'",
        ),
        (
            any(part.startswith(".") for part in parts),
            "Remove path components starting with '.'",
        ),
        (
            not (
                is_skill_md
                or is_resource
                or (
                    is_directory
                    and (
                        len(parts) == 1
                        or (len(parts) == 2 and parts[1] == RESOURCES_DIRNAME)
                    )
                )
            ),
            "Place files at <skill>/SKILL.md or <skill>/resources/<path>",
        ),
        (
            any(
                _normalized(part) == _normalized(SKILL_FILENAME)
                and not (index == 1 and is_skill_md)
                for index, part in enumerate(parts)
            ),
            "Use SKILL.md only at <skill>/SKILL.md with that exact casing",
        ),
        (
            any(
                _normalized(part) == _normalized(RESOURCES_DIRNAME)
                and part != RESOURCES_DIRNAME
                for part in parts
            ),
            "Spell every resources component as 'resources'",
        ),
    )
    return [
        Violation(Check.LAYOUT, path, message)
        for failed, message in rules
        if failed
    ]


def _check_name(name: str) -> list[Violation]:
    """Check a skill directory's spelling."""
    rules = (
        (
            not SKILL_NAME_RE.fullmatch(name),
            f"Skill name must match {SKILL_NAME_RE.pattern}",
        ),
        (
            len(name) > SKILL_NAME_MAX,
            f"Skill name must be at most {SKILL_NAME_MAX} characters",
        ),
        (
            name.startswith(RESERVED_PREFIXES),
            f"Skill name must not start with {RESERVED_PREFIXES!r}",
        ),
    )
    return [
        Violation(Check.NAME, name, message)
        for failed, message in rules
        if failed
    ]


def _check_limits(
    entries: list[Entry], skill_count: int, limits: Limits
) -> list[Violation]:
    """Check tree totals and the dimensions of every entry."""
    measurements = [
        ("", "Skill count", skill_count, limits.max_skills),
        ("", "Entry count", len(entries), limits.max_entries),
        (
            "",
            "Total size in bytes",
            sum(entry.size for entry in entries),
            limits.max_version_bytes,
        ),
    ]
    for entry in entries:
        parts = entry.path.split("/")
        measurements.extend(
            [
                (entry.path, "Path depth", len(parts), limits.max_depth),
                (
                    entry.path,
                    "Path length in characters",
                    len(entry.path),
                    limits.max_path_chars,
                ),
                (
                    entry.path,
                    "File size in bytes",
                    entry.size,
                    limits.max_file_bytes,
                ),
            ]
        )
        if (
            entry.mode in ALLOWED_MODES
            and len(parts) == 2
            and parts[1] == SKILL_FILENAME
        ):
            measurements.append(
                (
                    entry.path,
                    "SKILL.md size in bytes",
                    entry.size,
                    limits.max_skill_md_bytes,
                )
            )
    return [
        Violation(Check.LIMITS, path, f"{label} {actual} exceeds {maximum}")
        for path, label, actual, maximum in measurements
        if actual > maximum
    ]


def _check_collisions(entries: list[Entry]) -> list[Violation]:
    """Compare files and each directory component under normalized parents.

    Remember components separately from full paths: ancestor spelling aliases
    must neither duplicate child aliases nor hide distinct descendant aliases.
    """
    violations = []
    directories: dict[str, str] = {}
    components: dict[tuple[str, str], tuple[str, str]] = {}
    reported: set[tuple[str, str]] = set()
    files: dict[str, str] = {}
    for entry in entries:
        parts = entry.path.split("/")
        depth = len(parts) if entry.mode == "040000" else len(parts) - 1
        for index in range(depth):
            prefix = "/".join(parts[: index + 1])
            parent = _normalized("/".join(parts[:index]))
            key = (parent, _normalized(parts[index]))
            previous, original = components.setdefault(
                key, (parts[index], prefix)
            )
            directories.setdefault(_normalized(prefix), prefix)
            report = (_normalized(prefix), parts[index])
            # Skill aliases have their own check, regardless of occupancy.
            if index and previous != parts[index] and report not in reported:
                reported.add(report)
                violations.append(
                    Violation(
                        Check.COLLISION,
                        prefix,
                        f"Directory collides with {original!r} "
                        "after canonical caseless matching",
                    )
                )
        if entry.mode != "040000":
            key = _normalized(entry.path)
            if key in files:
                violations.append(
                    Violation(
                        Check.COLLISION,
                        entry.path,
                        f"Path collides with {files[key]!r} "
                        "after canonical caseless matching",
                    )
                )
            else:
                files[key] = entry.path
    for key, path in files.items():
        if key in directories:
            violations.append(
                Violation(
                    Check.COLLISION,
                    path,
                    f"File collides with directory {directories[key]!r} "
                    "after canonical caseless matching",
                )
            )
    return violations


def check_structure(
    team: str,
    entries: list[Entry],
    *,
    other_names: dict[str, str],
    limits: Limits,
) -> list[Violation]:
    """Report every layout, naming, collision, mode and limit violation."""
    if not TEAM_SLUG_RE.fullmatch(team):
        message = f"Team slug must match {TEAM_SLUG_RE.pattern}"
        raise ValueError(message)

    violations = []
    skill_counts: dict[str, int] = {}
    for entry in entries:
        path = entry.path
        parts = path.split("/")
        violations.extend(
            _check_layout(path, parts, is_directory=entry.mode == "040000")
        )
        if (
            not parts[0].startswith(".")
            and parts[0]
            and (len(parts) >= 2 or entry.mode == "040000")
        ):
            skill_counts.setdefault(parts[0], 0)
            if (
                len(parts) == 2
                and parts[1] == SKILL_FILENAME
                and entry.mode in ALLOWED_MODES
            ):
                skill_counts[parts[0]] += 1
        if entry.mode not in ALLOWED_MODES and entry.mode != "040000":
            violations.append(
                Violation(
                    Check.MODE,
                    path,
                    f"Mode {entry.mode} is forbidden, use 100644 or 100755",
                )
            )
        if any(_normalized(part) == "scripts" for part in parts):
            violations.append(
                Violation(Check.SCRIPTS, path, "Remove scripts components")
            )

    violations.extend(_check_collisions(entries))
    other_owners = {
        _normalized(name): owner for name, owner in other_names.items()
    }
    names: dict[str, str] = {}
    for name, count in skill_counts.items():
        key = _normalized(name)
        if key in names:
            violations.append(
                Violation(
                    Check.COLLISION,
                    name,
                    f"Skill name collides with {names[key]!r} "
                    "after canonical caseless matching",
                )
            )
        else:
            names[key] = name
        violations.extend(_check_name(name))
        if count != 1:
            violations.append(
                Violation(
                    Check.LAYOUT,
                    name,
                    f"Skill needs exactly one SKILL.md, found {count}",
                )
            )
        if key in other_owners:
            violations.append(
                Violation(
                    Check.COLLISION,
                    name,
                    f"Skill name {name!r} is already published by team "
                    f"{other_owners[key]}",
                )
            )
    violations.extend(_check_limits(entries, len(skill_counts), limits))
    return sorted(
        violations, key=lambda violation: (violation.check, violation.path)
    )
