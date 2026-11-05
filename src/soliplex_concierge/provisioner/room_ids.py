"""Ids for rooms the provisioner creates on request.

A provisioned room's id is ``<owner>:<slug>``: ``owner`` names the room's
owner, ``slug`` is the name the requester chose. A deployment may use the
id as, for instance, the room's directory name, its LanceDB stem, or its
uploads directory, so each part must be a safe single path segment of at
most :data:`MAX_SEGMENT` characters.

A :class:`RoomOwner` pairs the owner part with the access rule that grants
the owner the room: a soliplex ACL JSONPath over a user's claims (see
:mod:`soliplex_concierge.provisioner.access_rules`). The owner need not be
the requester (an admin may create a room for a group, say):
:func:`named_owner` names any owner, while :func:`owner_from_claims`
derives the requester's own.

A requester's owner part is the value of a configured claim (default
``preferred_username``), optionally *flattened* (``@`` -> ``_``) so
email-shaped values fit the segment rule. Flattening is neither invertible
nor injective, so the access rule matches the raw claim value, never the
owner part.

The slug is the requester's explicit choice or, by default, derived from
the room's human-readable name by :func:`slugify`.

Operator-curated rooms keep flat ids (no ``:``), so they never collide with
a provisioned one.

Stdlib only.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import re
import unicodedata

from soliplex_concierge.provisioner import access_rules

DEFAULT_SEPARATOR = "--"  # see #132
DEFAULT_OWNER_CLAIM = "preferred_username"

# One path segment: no '/', no leading dot (so neither '.' nor '..'). A
# segment may contain the room id separator; owners are built without it
# (see :func:`without_separator`) and slugs carrying it are refused, which
# keeps the split of an id into its parts unique.
SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Keeps '<owner>:<slug>.lancedb' well inside a 255-byte file name.
MAX_SEGMENT = 64
_SEGMENT_RULE = f"{SEGMENT_RE.pattern}, at most {MAX_SEGMENT} characters"

# What 'slugify' collapses into one '-'.
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


class RoomIdError(Exception):
    """A user-facing error (printed without a traceback)."""


class OwnerClaimMissing(RoomIdError):
    def __init__(self, claim):
        self.claim = claim
        super().__init__(f"the requester's token carries no {claim!r} claim")


class BadOwner(RoomIdError):
    def __init__(self, claim, value):
        self.claim, self.value = claim, value
        super().__init__(
            f"claim {claim!r} value {value!r} cannot own rooms: it must "
            f"match {_SEGMENT_RULE} (after any flattening)"
        )


class BadOwnerPart(RoomIdError):
    def __init__(self, owner):
        self.owner = owner
        super().__init__(f"room owner {owner!r} must match {_SEGMENT_RULE}")


class NoAccessRule(RoomIdError):
    def __init__(self, owner):
        self.owner = owner
        super().__init__(
            f"room owner {owner!r} needs an access rule (a JSONPath over a "
            "user's claims)"
        )


class BadSlug(RoomIdError):
    def __init__(self, slug, separator=DEFAULT_SEPARATOR):
        self.slug, self.separator = slug, separator
        super().__init__(
            f"room slug {slug!r} must match {_SEGMENT_RULE} and must not "
            f"contain {separator!r}"
        )


class SlugEmpty(RoomIdError):
    def __init__(self, name):
        self.name = name
        super().__init__(
            f"room name {name!r} yields no slug; choose one explicitly "
            f"({_SEGMENT_RULE})"
        )


class NotAProvisionedRoomId(RoomIdError):
    def __init__(self, room_id, separator):
        self.room_id = room_id
        self.separator = separator
        super().__init__(
            f"{room_id!r} is not a provisioned room id "
            f"('<owner>{separator}<name>')"
        )


@dataclasses.dataclass(frozen=True)
class RoomOwner:
    """Who owns a provisioned room.

    ``owner`` is the id part; ``json_path`` is the access rule granting the
    owner the room: a soliplex ACL JSONPath matching the owner's principals
    in a user's claims.
    """

    owner: str
    json_path: str


@dataclasses.dataclass(frozen=True)
class RoomId:
    """A provisioned room's id, split into its parts."""

    owner: str
    slug: str
    separator: str = DEFAULT_SEPARATOR

    def __str__(self) -> str:
        return f"{self.owner}{self.separator}{self.slug}"


def is_segment(value: str) -> bool:
    """Tell whether ``value`` is usable as one part of a room id."""
    return len(value) <= MAX_SEGMENT and bool(SEGMENT_RE.fullmatch(value))


def flatten(value: str) -> str:
    """Map an email-shaped claim value onto the segment character set."""
    return value.replace("@", "_")


def without_separator(value: str, separator: str = DEFAULT_SEPARATOR) -> str:
    """Replace each ``separator`` in ``value``, so it can be an owner part.

    The replacement is ``-``, or ``_`` when the separator contains ``-``;
    it repeats until no ``separator`` is left (a replacement can join its
    neighbours into a new one).
    """
    replacement = "_" if "-" in separator else "-"
    while separator and separator in value:
        value = value.replace(separator, replacement)
    return value


def named_owner(
    owner: str, json_path: str, separator: str = DEFAULT_SEPARATOR
) -> RoomOwner:
    """Name a room's owner explicitly: its id part, and its access rule.

    Each ``separator`` in ``owner`` is replaced (see
    :func:`without_separator`). Raises :class:`BadOwnerPart` when the
    result does not fit the segment rule, and :class:`NoAccessRule` when
    ``json_path`` is blank.
    """
    owner = without_separator(owner, separator)
    if not is_segment(owner):
        raise BadOwnerPart(owner)
    if not json_path.strip():
        raise NoAccessRule(owner)
    return RoomOwner(owner=owner, json_path=json_path)


def owner_from_claims(
    claims: collections.abc.Mapping,
    *,
    claim: str = DEFAULT_OWNER_CLAIM,
    flatten_value: bool = False,
    separator: str = DEFAULT_SEPARATOR,
) -> RoomOwner:
    """Derive the requester's own room owner from their token ``claims``.

    Reads ``claims[claim]``, flattening it when ``flatten_value`` is true
    and replacing each ``separator`` (see :func:`without_separator`); the
    access rule matches the raw value. Raises
    :class:`access_rules.BadOwnerClaim` when ``claim`` is not an
    identifier, :class:`OwnerClaimMissing` when the claim is absent, and
    :class:`BadOwner` when its value is not a string or does not fit the
    segment rule.
    """
    try:
        value, json_path = access_rules.claim_value_rule(claims, claim)
    except access_rules.ClaimMissing as error:
        raise OwnerClaimMissing(claim) from error
    except access_rules.ClaimNotAString as error:
        raise BadOwner(claim, error.value) from error

    owner = flatten(value) if flatten_value else value
    owner = without_separator(owner, separator)
    if not is_segment(owner):
        raise BadOwner(claim, value)

    return RoomOwner(owner=owner, json_path=json_path)


def validate_slug(slug: str, separator: str = DEFAULT_SEPARATOR) -> None:
    """Raise :class:`BadSlug` unless ``slug`` fits the segment rule.

    A slug carrying ``separator`` is refused too (:func:`slugify` never
    yields one).
    """
    if not is_segment(slug) or (separator and separator in slug):
        raise BadSlug(slug, separator)


def slugify(name: str) -> str:
    """Derive a slug from a room's human-readable ``name``.

    Drops accents, lowercases, collapses each run of other characters into
    one ``-``, strips ``-`` from both ends, and truncates to
    :data:`MAX_SEGMENT`. Raises :class:`SlugEmpty` when nothing is left.
    """
    ascii_name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _NON_SLUG_RE.sub("-", ascii_name.lower()).strip("-")
    slug = slug[:MAX_SEGMENT].rstrip("-")
    if not slug:
        raise SlugEmpty(name)
    return slug


def make_room_id(
    owner: RoomOwner,
    slug: str,
    separator: str = DEFAULT_SEPARATOR,
) -> RoomId:
    """Compose the id of ``owner``'s room named ``slug``."""
    validate_slug(slug, separator)
    return RoomId(owner=owner.owner, slug=slug, separator=separator)


def parse_room_id(room_id: str, separator: str = DEFAULT_SEPARATOR) -> RoomId:
    """Split a provisioned room id into its parts.

    Raises :class:`NotAProvisionedRoomId` unless ``room_id`` is exactly two
    segments joined by :data:`separator`.
    """
    owner, sep, slug = room_id.partition(separator)
    if not (sep and is_segment(owner) and is_segment(slug)):
        raise NotAProvisionedRoomId(room_id, separator)
    return RoomId(owner=owner, slug=slug, separator=separator)
