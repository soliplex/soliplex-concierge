"""The data a skills deploy passes between its steps.

A team's repository is read from git objects (:class:`Entry`), checked
(:class:`Violation`), and written out as an immutable version
(:class:`Version`). :class:`Limits` bounds what one team may deploy.
Checks use stable identifiers C1-C9.
"""

import collections.abc
import dataclasses
import enum
import pathlib
import re
import types

#: Skill names Soliplex accepts (``soliplex/capabilities/filesystem.py``).
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: The longest skill name Soliplex accepts.
SKILL_NAME_MAX = 64
#: Team slugs use the skill-name spelling rules.
TEAM_SLUG_RE = SKILL_NAME_RE
#: Name prefixes no team may use.
RESERVED_PREFIXES = ("soliplex-",)

SKILL_FILENAME = "SKILL.md"
RESOURCES_DIRNAME = "resources"
#: Git modes of the blobs a team may deploy (regular files).
ALLOWED_MODES = frozenset({"100644", "100755"})
#: Frontmatter keys a team may set.
FRONTMATTER_KEYS = frozenset({"name", "description", "visibility"})
#: Values of the ``visibility`` frontmatter key.
VISIBILITIES = frozenset({"public", "private"})
#: Visibility when the frontmatter does not set it.
DEFAULT_VISIBILITY = "private"
#: The longest ``description`` Soliplex accepts.
DESCRIPTION_MAX = 1024


class Check(enum.StrEnum):
    """The check a :class:`Violation` failed."""

    LAYOUT = "C1"
    NAME = "C2"
    COLLISION = "C3"
    FRONTMATTER = "C4"
    MODE = "C5"
    SCRIPTS = "C6"
    ENCODING = "C7"
    LIMITS = "C8"
    GENERATION = "C9"


@dataclasses.dataclass(frozen=True)
class Limits:
    """What one team may deploy (C8)."""

    max_skills: int = 50
    max_entries: int = 500
    max_depth: int = 8
    max_path_chars: int = 200
    max_skill_md_bytes: int = 64 * 1024
    max_file_bytes: int = 1024 * 1024
    max_version_bytes: int = 20 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class Entry:
    """One Git entry: regular file, tree, symlink, or gitlink."""

    mode: str
    oid: str
    size: int
    path: str  # repository-relative, ``/``-separated


@dataclasses.dataclass(frozen=True)
class Violation:
    """One failed check, in words a team can act on."""

    check: Check
    path: str  # the offending path, or "" for the whole tree
    message: str


@dataclasses.dataclass(frozen=True)
class Skill:
    """A skill that passed every content check."""

    name: str
    description: str
    visibility: str
    instructions_digest: str  # sha256 of the normalised instructions


@dataclasses.dataclass(frozen=True)
class Version:
    """A team's skills written out, immutable, at one revision."""

    team: str
    revision: str
    path: pathlib.Path
    skills: tuple[Skill, ...]
    blob_digests: collections.abc.Mapping[str, str]  # path -> sha256

    def __post_init__(self):
        object.__setattr__(
            self,
            "blob_digests",
            types.MappingProxyType(dict(self.blob_digests)),
        )
