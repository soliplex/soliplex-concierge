"""Ids for rooms the provisioner creates on request.

A provisioned room's id is ``<owner><separator><slug>`` (by default
``<owner>--<slug>``): ``owner`` names the room's owner, ``slug`` names the
room among the owner's rooms, and the separator is the deployment's. A
deployment may use the id as, for instance, the room's directory name, its
LanceDB stem, or its uploads directory, so each part must be a safe single
path segment.

A :class:`RoomIdScheme` holds a deployment's rules for its ids: the
separator, the longest part, and how an owner part derives from a claim or
a name (see :attr:`provision.Provisioner.scheme`). Its methods build, check
and split ids under those rules.

An id splits into its parts one way only (#143):

- each part starts and ends with a letter or digit (:data:`SEGMENT_RE`);
- a separator holds no letter or digit (:func:`validate_separator`);
- neither part holds the separator: an owner part has each one replaced,
  and any trailing ``.``, ``_`` or ``-`` stripped; a slug holding one is
  refused.

So no occurrence of the separator can straddle the join, and the first one
is the join.

A :class:`RoomOwner` pairs the owner part with the access rule that grants
the owner the room: a soliplex ACL JSONPath over a user's claims (see
:mod:`soliplex_concierge.provisioner.access_rules`). The owner need not be
the requester (an admin may create a room for a group, say):
:meth:`RoomIdScheme.named_owner` names any owner, while
:meth:`RoomIdScheme.owner_from_claims` derives the requester's own.

A requester's owner part is the value of a configured claim (default
``preferred_username``), optionally *flattened* (``@`` -> ``_``) so
email-shaped values fit the segment rule, and lowercased by default, so
ids differing only by case never land in one directory on a
case-insensitive file system. None of these steps is invertible nor
injective, so the access rule matches the raw claim value, never the
owner part.

The requester gives the room a human-readable name (its title), from which
:meth:`RoomIdScheme.slugify` derives the slug; its output never contains a
valid separator. A requester who asks for a specific slug gets it when it
fits (:meth:`RoomIdScheme.validate_slug`); otherwise the request is
refused, so they can retry with another.

Operator-curated rooms keep flat ids (no separator), so they never collide
with a provisioned one.

The module-level functions taking a ``separator`` (``make_room_id``,
``parse_room_id``, ...) are deprecated shims over :class:`RoomIdScheme`,
kept until after v0.16.

Stdlib only.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import re
import unicodedata
import warnings

from soliplex_concierge.provisioner import access_rules

DEFAULT_SEPARATOR = "--"  # see #132
DEFAULT_OWNER_CLAIM = "preferred_username"

# One path segment, starting and ending with a letter or digit: no '/', no
# leading dot (so neither '.' nor '..'), and nothing a separator could
# share at either end (see #143).
SEGMENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# Keeps '<owner><separator><slug>.lancedb' well inside a 255-byte file name;
# a scheme may lower it, never raise it.
MAX_SEGMENT = 64
MAX_SEPARATOR = 8

# What 'slugify' collapses into one '-'.
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
# What an owner part loses from its end: the segment punctuation the
# segment rule refuses there (see #143).
_TRAILING_RE = re.compile(r"[._-]+\Z")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
# Never in a separator: path separators, and what Windows file names, URIs
# or S3 keys refuse or treat specially. ':' stays allowed, so a deployment
# can keep the legacy separator.
_UNSAFE_SEPARATOR_CHARS = frozenset('/\\<>"|?*#%')


def _segment_rule(max_segment: int = MAX_SEGMENT) -> str:
    return f"{SEGMENT_RE.pattern}, at most {max_segment} characters"


def _deprecated(old: str, new: str) -> None:
    warnings.warn(
        f"{old} is deprecated: use {new} instead (it will not be removed "
        "until after v0.16)",
        DeprecationWarning,
        stacklevel=3,
    )


class RoomIdError(Exception):
    """A user-facing error (printed without a traceback)."""


class OwnerClaimMissing(RoomIdError):
    def __init__(self, claim):
        self.claim = claim
        super().__init__(f"the requester's token carries no {claim!r} claim")


class BadOwner(RoomIdError):
    def __init__(self, claim, value, max_segment=MAX_SEGMENT):
        self.claim, self.value = claim, value
        super().__init__(
            f"claim {claim!r} value {value!r} cannot own rooms: it must "
            f"match {_segment_rule(max_segment)} (after any flattening)"
        )


class BadOwnerPart(RoomIdError):
    def __init__(self, owner, max_segment=MAX_SEGMENT, separator=None):
        self.owner = owner
        rule = f"match {_segment_rule(max_segment)}"
        if separator is not None:
            rule += f" and must not contain {separator!r}"
        super().__init__(f"room owner {owner!r} must {rule}")


class NoAccessRule(RoomIdError):
    def __init__(self, owner):
        self.owner = owner
        super().__init__(
            f"room owner {owner!r} needs an access rule (a JSONPath over a "
            "user's claims)"
        )


class BadSlug(RoomIdError):
    def __init__(
        self, slug, separator=DEFAULT_SEPARATOR, max_segment=MAX_SEGMENT
    ):
        self.slug, self.separator = slug, separator
        super().__init__(
            f"room slug {slug!r} must match {_segment_rule(max_segment)} and "
            f"must not contain {separator!r}"
        )


class BadSeparator(RoomIdError):
    def __init__(self, separator, reason):
        self.separator, self.reason = separator, reason
        super().__init__(
            f"room id separator {separator!r} is unusable: {reason}"
        )


class BadMaxSegment(RoomIdError):
    def __init__(self, max_segment):
        self.max_segment = max_segment
        super().__init__(
            f"a room id part's longest length {max_segment!r} must be "
            f"between 1 and {MAX_SEGMENT}"
        )


class SlugEmpty(RoomIdError):
    def __init__(self, name, max_segment=MAX_SEGMENT):
        self.name = name
        super().__init__(
            f"room name {name!r} yields no slug; choose one explicitly "
            f"({_segment_rule(max_segment)})"
        )


class SchemeConflict(TypeError):
    """Both a scheme and the deprecated settings it replaces were given."""

    def __init__(self, deprecated):
        self.deprecated = deprecated
        super().__init__(
            f"pass 'scheme', or the deprecated {deprecated}, not both"
        )


class NotAProvisionedRoomId(RoomIdError):
    def __init__(self, room_id, separator):
        self.room_id = room_id
        self.separator = separator
        super().__init__(
            f"{room_id!r} is not a provisioned room id "
            f"('<owner>{separator}<name>')"
        )


def flatten(value: str) -> str:
    """Map an email-shaped claim value onto the segment character set."""
    return value.replace("@", "_")


def validate_separator(separator: str) -> None:
    """Raise :class:`BadSeparator` unless ``separator`` can join room ids.

    It must be printable ASCII of 1 to :data:`MAX_SEPARATOR` characters,
    hold no whitespace, no ``/ \\ < > " | ? * # %``, and no letter or digit
    (so no occurrence of it can straddle a part's end; see #143), and must
    not be a lone ``-`` (which a :func:`slugify` output can contain).
    """
    if not separator:
        raise BadSeparator(separator, "it is empty")
    if len(separator) > MAX_SEPARATOR:
        raise BadSeparator(
            separator, f"it is longer than {MAX_SEPARATOR} characters"
        )
    unsafe = sorted(
        {
            char
            for char in separator
            if char in _UNSAFE_SEPARATOR_CHARS
            or not char.isascii()
            or not char.isprintable()
            or char.isspace()
        }
    )
    if unsafe:
        raise BadSeparator(separator, f"it contains {''.join(unsafe)!r}")
    if _ALNUM_RE.search(separator):
        raise BadSeparator(separator, "it contains a letter or digit")
    if separator == "-":
        raise BadSeparator(
            separator, "slugs derived from room names can contain it"
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
class RoomIdScheme:
    """A deployment's rules for its room ids.

    - ``separator`` joins the owner part and the slug (default
      :data:`DEFAULT_SEPARATOR`; ``":"`` keeps the legacy form).

    - ``max_segment`` bounds each part's length (1 to :data:`MAX_SEGMENT`).

    - ``lowercase_owner`` lowercases an owner part, whether derived from
      claims or named.

    - ``flatten_owner`` flattens an owner part derived from claims (see
      :func:`flatten`).

    Raises :class:`BadSeparator` for an unusable separator (see
    :func:`validate_separator`), and :class:`BadMaxSegment` for an
    unusable ``max_segment``.
    """

    separator: str = DEFAULT_SEPARATOR
    max_segment: int = MAX_SEGMENT
    lowercase_owner: bool = True
    flatten_owner: bool = False

    def __post_init__(self) -> None:
        validate_separator(self.separator)
        if not 1 <= self.max_segment <= MAX_SEGMENT:
            raise BadMaxSegment(self.max_segment)

    def is_segment(self, value: str) -> bool:
        """Tell whether ``value`` is usable as one part of a room id."""
        return len(value) <= self.max_segment and bool(
            SEGMENT_RE.fullmatch(value)
        )

    def without_separator(self, value: str) -> str:
        """Replace each separator in ``value``, so it can be an owner part.

        The replacement is ``-``, or ``_`` when the separator contains
        ``-``; it repeats until no separator is left (a replacement can
        join its neighbours into a new one).
        """
        replacement = "_" if "-" in self.separator else "-"
        while self.separator in value:
            value = value.replace(self.separator, replacement)
        return value

    def _owner_part(self, value: str, *, flatten_value: bool) -> str:
        if flatten_value:
            value = flatten(value)
        if self.lowercase_owner:
            value = value.lower()
        return _TRAILING_RE.sub("", self.without_separator(value))

    def named_owner(self, owner: str, json_path: str) -> RoomOwner:
        """Name a room's owner explicitly: its id part, and its access rule.

        ``owner`` is lowercased when ``lowercase_owner`` is set, each
        separator in it replaced (see :meth:`without_separator`), and any
        trailing ``.``, ``_`` or ``-`` stripped. Raises :class:`BadOwnerPart`
        when the result does not fit the segment rule, and
        :class:`NoAccessRule` when ``json_path`` is blank.
        """
        part = self._owner_part(owner, flatten_value=False)
        if not self.is_segment(part):
            raise BadOwnerPart(owner, self.max_segment)
        if not json_path.strip():
            raise NoAccessRule(part)
        return RoomOwner(owner=part, json_path=json_path)

    def owner_from_claims(
        self,
        claims: collections.abc.Mapping,
        *,
        claim: str = DEFAULT_OWNER_CLAIM,
    ) -> RoomOwner:
        """Derive the requester's own room owner from their token ``claims``.

        Reads ``claims[claim]``, then flattens it when ``flatten_owner`` is
        set, lowercases it when ``lowercase_owner`` is set, replaces each
        separator (see :meth:`without_separator`), and strips any trailing
        ``.``, ``_`` or ``-``; the access rule matches the raw value. Raises
        :class:`access_rules.BadOwnerClaim` when ``claim`` is not an
        identifier, :class:`OwnerClaimMissing` when the claim is absent,
        and :class:`BadOwner` when its value is not a string or does not
        fit the segment rule.
        """
        try:
            value, json_path = access_rules.claim_value_rule(claims, claim)
        except access_rules.ClaimMissing as error:
            raise OwnerClaimMissing(claim) from error
        except access_rules.ClaimNotAString as error:
            raise BadOwner(claim, error.value, self.max_segment) from error

        part = self._owner_part(value, flatten_value=self.flatten_owner)
        if not self.is_segment(part):
            raise BadOwner(claim, value, self.max_segment)

        return RoomOwner(owner=part, json_path=json_path)

    def validate_slug(self, slug: str) -> None:
        """Raise :class:`BadSlug` unless ``slug`` fits the segment rule.

        A slug carrying the separator is refused too (:meth:`slugify`
        never yields one).
        """
        if not self.is_segment(slug) or self.separator in slug:
            raise BadSlug(slug, self.separator, self.max_segment)

    def slugify(self, name: str) -> str:
        """Derive a slug from a room's human-readable ``name``.

        Drops accents, lowercases, collapses each run of other characters
        into one ``-``, strips ``-`` from both ends, and truncates to
        ``max_segment``. Raises :class:`SlugEmpty` when nothing is left.
        """
        ascii_name = (
            unicodedata.normalize("NFKD", name)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        slug = _NON_SLUG_RE.sub("-", ascii_name.lower()).strip("-")
        slug = slug[: self.max_segment].rstrip("-")
        if not slug:
            raise SlugEmpty(name, self.max_segment)
        return slug

    def make_room_id(self, owner: RoomOwner, slug: str) -> RoomId:
        """Compose the id of ``owner``'s room named ``slug``.

        Raises :class:`BadOwnerPart` for an owner part this scheme cannot
        have made (one not fitting the segment rule, or holding the
        separator), and :class:`BadSlug` for an unusable slug.
        """
        if not self.is_segment(owner.owner) or self.separator in owner.owner:
            raise BadOwnerPart(owner.owner, self.max_segment, self.separator)
        self.validate_slug(slug)
        return RoomId(owner=owner.owner, slug=slug, scheme=self)

    def parse_room_id(self, room_id: str) -> RoomId:
        """Split a provisioned room id into its parts.

        Raises :class:`NotAProvisionedRoomId` unless ``room_id`` is
        exactly two segments joined by the separator.
        """
        owner, sep, slug = room_id.partition(self.separator)
        if not (
            sep
            and self.is_segment(owner)
            and self.is_segment(slug)
            and self.separator not in slug
        ):
            raise NotAProvisionedRoomId(room_id, self.separator)
        return RoomId(owner=owner, slug=slug, scheme=self)


DEFAULT_SCHEME = RoomIdScheme()


@dataclasses.dataclass(frozen=True, init=False)
class RoomId:
    """A provisioned room's id, split into its parts, under its scheme.

    Two ids are equal only under equal schemes. ``RoomId(owner, slug,
    separator=...)`` is a deprecated spelling of ``RoomId(owner, slug,
    RoomIdScheme(separator=...))``.
    """

    owner: str
    slug: str
    scheme: RoomIdScheme = dataclasses.field(repr=False)

    def __init__(
        self,
        owner: str,
        slug: str,
        scheme: RoomIdScheme | str | None = None,
        *,
        separator: str | None = None,
    ) -> None:
        if isinstance(scheme, str):  # the deprecated positional separator
            scheme, separator = None, scheme
        if separator is not None:
            if scheme is not None:
                raise SchemeConflict("'separator'")
            _deprecated(
                "RoomId(separator=...)",
                "RoomId(scheme=RoomIdScheme(separator=...))",
            )
            scheme = RoomIdScheme(separator=separator)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "scheme", scheme or DEFAULT_SCHEME)

    @property
    def separator(self) -> str:
        return self.scheme.separator

    def __str__(self) -> str:
        return f"{self.owner}{self.separator}{self.slug}"


# --------------------------------------------------------------------------
# Deprecated shims over RoomIdScheme (not removed until after v0.16)
# --------------------------------------------------------------------------


def is_segment(value: str) -> bool:
    """Deprecated: use :meth:`RoomIdScheme.is_segment`."""
    _deprecated("room_ids.is_segment", "RoomIdScheme.is_segment")
    return DEFAULT_SCHEME.is_segment(value)


def without_separator(value: str, separator: str = DEFAULT_SEPARATOR) -> str:
    """Deprecated: use :meth:`RoomIdScheme.without_separator`."""
    _deprecated("room_ids.without_separator", "RoomIdScheme.without_separator")
    return RoomIdScheme(separator=separator).without_separator(value)


def named_owner(
    owner: str,
    json_path: str,
    separator: str = DEFAULT_SEPARATOR,
    lowercase: bool = True,
) -> RoomOwner:
    """Deprecated: use :meth:`RoomIdScheme.named_owner`."""
    _deprecated("room_ids.named_owner", "RoomIdScheme.named_owner")
    scheme = RoomIdScheme(separator=separator, lowercase_owner=lowercase)
    return scheme.named_owner(owner, json_path)


def owner_from_claims(
    claims: collections.abc.Mapping,
    *,
    claim: str = DEFAULT_OWNER_CLAIM,
    flatten_value: bool = False,
    lowercase_value: bool = True,
    separator: str = DEFAULT_SEPARATOR,
) -> RoomOwner:
    """Deprecated: use :meth:`RoomIdScheme.owner_from_claims`."""
    _deprecated("room_ids.owner_from_claims", "RoomIdScheme.owner_from_claims")
    scheme = RoomIdScheme(
        separator=separator,
        lowercase_owner=lowercase_value,
        flatten_owner=flatten_value,
    )
    return scheme.owner_from_claims(claims, claim=claim)


def validate_slug(slug: str, separator: str = DEFAULT_SEPARATOR) -> None:
    """Deprecated: use :meth:`RoomIdScheme.validate_slug`."""
    _deprecated("room_ids.validate_slug", "RoomIdScheme.validate_slug")
    RoomIdScheme(separator=separator).validate_slug(slug)


def slugify(name: str) -> str:
    """Deprecated: use :meth:`RoomIdScheme.slugify`."""
    _deprecated("room_ids.slugify", "RoomIdScheme.slugify")
    return DEFAULT_SCHEME.slugify(name)


def make_room_id(
    owner: RoomOwner,
    slug: str,
    separator: str = DEFAULT_SEPARATOR,
) -> RoomId:
    """Deprecated: use :meth:`RoomIdScheme.make_room_id`."""
    _deprecated("room_ids.make_room_id", "RoomIdScheme.make_room_id")
    return RoomIdScheme(separator=separator).make_room_id(owner, slug)


def parse_room_id(room_id: str, separator: str = DEFAULT_SEPARATOR) -> RoomId:
    """Deprecated: use :meth:`RoomIdScheme.parse_room_id`."""
    _deprecated("room_ids.parse_room_id", "RoomIdScheme.parse_room_id")
    return RoomIdScheme(separator=separator).parse_room_id(room_id)
