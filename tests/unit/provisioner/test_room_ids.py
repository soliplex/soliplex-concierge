"""Unit tests for ``soliplex_concierge.provisioner.room_ids``.

Each test is laid out in three blank-line-separated phases -- setup, then the
single call under test (the "act"), then the assertions -- and performs that
act exactly once (cases that would repeat it are parametrized or split).
"""

from __future__ import annotations

import itertools

import pytest

from soliplex_concierge.provisioner import access_rules
from soliplex_concierge.provisioner import room_ids

SCHEME = room_ids.DEFAULT_SCHEME
COLON = room_ids.RoomIdScheme(separator=":")
UNDERSCORES = room_ids.RoomIdScheme(separator="__")

# Values that are not one safe path segment.
_BAD_SEGMENTS = [
    "",
    ".",
    "..",
    ".hidden",
    "-leading-dash",
    "a/b",
    "a:b",
    "with space",
    "trailing-newline\n",
    "ünïcode",
    "a" * (room_ids.MAX_SEGMENT + 1),
]
# Segments the old rule allowed, which now end only with a letter or digit.
_BAD_ENDINGS = ["bob-", "bob_", "bob."]

_RULE = '$[?$.preferred_username == "alice"]'
_GROUP_RULE = "$[?'staff' in @.groups]"


def _owner(owner="alice"):
    return room_ids.RoomOwner(owner=owner, json_path=_RULE)


# ---- validate_separator ----


@pytest.mark.parametrize(
    "separator",
    [room_ids.DEFAULT_SEPARATOR, ":", "__", "_", "~", "-_", "---", "." * 8],
)
def test_validate_separator_accepts(separator):
    result = room_ids.validate_separator(separator)

    assert result is None


@pytest.mark.parametrize(
    "separator, reason",
    [
        ("", "it is empty"),
        ("~" * (room_ids.MAX_SEPARATOR + 1), "it is longer than 8 characters"),
        ("/", "it contains '/'"),
        ("\\", "it contains '\\\\'"),
        ("?#", "it contains '#?'"),
        ("%", "it contains '%'"),
        (" ", "it contains ' '"),
        ("\t", "it contains '\\t'"),
        ("\x00", "it contains '\\x00'"),
        ("→", "it contains '→'"),
        ("x", "it contains a letter or digit"),
        ("X", "it contains a letter or digit"),
        ("a--b", "it contains a letter or digit"),
        ("-1-", "it contains a letter or digit"),
        ("-", "slugs derived from room names can contain it"),
    ],
)
def test_validate_separator_rejects(separator, reason):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.validate_separator(separator)

    assert type(caught.value) is room_ids.BadSeparator
    assert caught.value.separator == separator
    assert caught.value.reason == reason
    assert str(caught.value) == (
        f"room id separator {separator!r} is unusable: {reason}"
    )


# ---- flatten ----


@pytest.mark.parametrize(
    "value, expected",
    [
        ("bharney", "bharney"),
        ("bharney@sitename.example", "bharney_sitename.example"),
        ("a@b@c", "a_b_c"),
    ],
)
def test_flatten(value, expected):
    result = room_ids.flatten(value)

    assert result == expected


# ---- RoomIdScheme ----


def test_scheme_defaults():
    result = room_ids.RoomIdScheme()

    assert result.separator == room_ids.DEFAULT_SEPARATOR
    assert result.max_segment == room_ids.MAX_SEGMENT
    assert result.lowercase_owner is True
    assert result.flatten_owner is False


def test_scheme_rejects_a_bad_separator():
    with pytest.raises(room_ids.BadSeparator) as caught:
        room_ids.RoomIdScheme(separator="X")

    assert caught.value.separator == "X"


@pytest.mark.parametrize("max_segment", [0, room_ids.MAX_SEGMENT + 1])
def test_scheme_rejects_a_bad_max_segment(max_segment):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.RoomIdScheme(max_segment=max_segment)

    assert type(caught.value) is room_ids.BadMaxSegment
    assert caught.value.max_segment == max_segment


# ---- RoomIdScheme.is_segment ----


@pytest.mark.parametrize(
    "value, expected",
    [
        ("alice", True),
        ("a", True),
        ("v1.2_draft", True),
        ("a" * room_ids.MAX_SEGMENT, True),
        *[(bad, False) for bad in _BAD_SEGMENTS + _BAD_ENDINGS],
    ],
)
def test_is_segment(value, expected):
    result = SCHEME.is_segment(value)

    assert result is expected


def test_is_segment_honors_max_segment():
    scheme = room_ids.RoomIdScheme(max_segment=3)

    result = scheme.is_segment("abcd")

    assert result is False


# ---- RoomIdScheme.without_separator ----


@pytest.mark.parametrize(
    "scheme, value, expected",
    [
        (SCHEME, "alice", "alice"),
        (SCHEME, "a--b", "a_b"),
        (SCHEME, "a---b", "a_-b"),
        (SCHEME, "a----b", "a__b"),
        (COLON, "a:b", "a-b"),
        (COLON, "a::b", "a--b"),
        (UNDERSCORES, "a__b", "a-b"),
        (UNDERSCORES, "a___b", "a-_b"),
        (room_ids.RoomIdScheme(separator="-_"), "a-_-_b", "a__b"),
    ],
)
def test_without_separator(scheme, value, expected):
    result = scheme.without_separator(value)

    assert result == expected


# ---- RoomIdScheme.named_owner ----


def test_named_owner():
    result = SCHEME.named_owner("staff", _GROUP_RULE)

    assert result == room_ids.RoomOwner(owner="staff", json_path=_GROUP_RULE)


@pytest.mark.parametrize(
    "scheme, owner, expected",
    [
        (SCHEME, "night--shift", "night_shift"),
        (COLON, "night:shift", "night-shift"),
        (SCHEME, "Night--Shift", "night_shift"),
        (
            room_ids.RoomIdScheme(lowercase_owner=False),
            "Night--Shift",
            "Night_Shift",
        ),
        (SCHEME, "ops-", "ops"),
        (SCHEME, "ops_.-", "ops"),
        (SCHEME, "ops--", "ops"),
        (UNDERSCORES, "ops_", "ops"),
        (room_ids.RoomIdScheme(flatten_owner=True), "a_b", "a_b"),
    ],
    ids=[
        "separator",
        "colon",
        "lowercased",
        "not-lowercased",
        "trailing-dash",
        "trailing-run",
        "trailing-separator",
        "trailing-underscore",
        "never-flattened",
    ],
)
def test_named_owner_normalizes(scheme, owner, expected):
    result = scheme.named_owner(owner, _GROUP_RULE)

    assert result.owner == expected


@pytest.mark.parametrize(
    "owner",
    [
        *_BAD_SEGMENTS,
        "a@b",
        "--",
        "_",
        "bob\N{LATIN SMALL LETTER E WITH ACUTE}",
    ],
)
def test_named_owner_rejects_a_bad_owner_part(owner):
    with pytest.raises(room_ids.BadOwnerPart) as caught:
        SCHEME.named_owner(owner, _GROUP_RULE)

    assert caught.value.owner == owner


@pytest.mark.parametrize("json_path", ["", "   "])
def test_named_owner_needs_an_access_rule(json_path):
    with pytest.raises(room_ids.NoAccessRule) as caught:
        SCHEME.named_owner("staff", json_path)

    assert caught.value.owner == "staff"
    assert "needs an access rule" in str(caught.value)


# ---- RoomIdScheme.owner_from_claims ----

_FLATTEN = room_ids.RoomIdScheme(flatten_owner=True)


@pytest.mark.parametrize(
    "scheme, claims, claim, expected_owner",
    [
        (SCHEME, {"preferred_username": "alice"}, None, "alice"),
        (SCHEME, {"preferred_username": "12345678"}, None, "12345678"),
        (
            _FLATTEN,
            {"email": "bharney@sitename.example"},
            "email",
            "bharney_sitename.example",
        ),
        (SCHEME, {"preferred_username": "jo--smith"}, None, "jo_smith"),
        (SCHEME, {"preferred_username": "Alice"}, None, "alice"),
        (
            room_ids.RoomIdScheme(lowercase_owner=False),
            {"preferred_username": "Alice"},
            None,
            "Alice",
        ),
        (
            _FLATTEN,
            {"email": "Jo.Smith@Example.COM"},
            "email",
            "jo.smith_example.com",
        ),
        (COLON, {"preferred_username": "jo:smith"}, None, "jo-smith"),
        (
            _FLATTEN,
            {"email": "jo--smith@sitename.example"},
            "email",
            "jo_smith_sitename.example",
        ),
        (SCHEME, {"preferred_username": "bob-"}, None, "bob"),
        (UNDERSCORES, {"preferred_username": "bob_"}, None, "bob"),
        (_FLATTEN, {"email": "bob@"}, "email", "bob"),
    ],
)
def test_owner_from_claims(scheme, claims, claim, expected_owner):
    kwargs = {} if claim is None else {"claim": claim}
    claim = claim or room_ids.DEFAULT_OWNER_CLAIM

    result = scheme.owner_from_claims(claims, **kwargs)

    assert result == room_ids.RoomOwner(
        owner=expected_owner,
        json_path=f'$[?$.{claim} == "{claims[claim]}"]',
    )


def test_owner_from_claims_rejects_a_bad_claim():
    claims = {"a.b": "alice"}

    with pytest.raises(access_rules.BadOwnerClaim) as caught:
        SCHEME.owner_from_claims(claims, claim="a.b")

    assert caught.value.claim == "a.b"


def test_owner_from_claims_requires_the_claim():
    claims = {"email": "alice@example.com"}

    with pytest.raises(room_ids.RoomIdError) as caught:
        SCHEME.owner_from_claims(claims)

    assert type(caught.value) is room_ids.OwnerClaimMissing
    assert caught.value.claim == "preferred_username"
    assert "'preferred_username'" in str(caught.value)


@pytest.mark.parametrize(
    "claims, claim",
    [
        ({"preferred_username": 12345}, None),
        ({"preferred_username": None}, None),
        ({"email": "alice@example.com"}, "email"),
        *[({"preferred_username": bad}, None) for bad in _BAD_SEGMENTS],
    ],
)
def test_owner_from_claims_rejects_unusable_values(claims, claim):
    kwargs = {} if claim is None else {"claim": claim}
    claim = claim or room_ids.DEFAULT_OWNER_CLAIM

    with pytest.raises(room_ids.RoomIdError) as caught:
        SCHEME.owner_from_claims(claims, **kwargs)

    assert type(caught.value) is room_ids.BadOwner
    assert caught.value.claim == claim
    assert caught.value.value == claims[claim]
    assert "cannot own rooms" in str(caught.value)


# ---- RoomIdScheme.validate_slug ----


@pytest.mark.parametrize(
    "scheme, slug",
    [
        (SCHEME, "notes"),
        (SCHEME, "Q3-plans"),
        (SCHEME, "v1.2_draft"),
        (SCHEME, "7"),
        (SCHEME, "a" * room_ids.MAX_SEGMENT),
        (COLON, "q3--plans"),
        (SCHEME, "q3__plans"),
    ],
)
def test_validate_slug_accepts(scheme, slug):
    result = scheme.validate_slug(slug)

    assert result is None


@pytest.mark.parametrize("slug", _BAD_SEGMENTS + _BAD_ENDINGS)
def test_validate_slug_rejects(slug):
    with pytest.raises(room_ids.RoomIdError) as caught:
        SCHEME.validate_slug(slug)

    assert type(caught.value) is room_ids.BadSlug
    assert caught.value.slug == slug


@pytest.mark.parametrize(
    "scheme, slug",
    [(SCHEME, "q3--plans"), (UNDERSCORES, "q3__plans")],
)
def test_validate_slug_rejects_the_separator(scheme, slug):
    with pytest.raises(room_ids.BadSlug) as caught:
        scheme.validate_slug(slug)

    assert caught.value.slug == slug
    assert caught.value.separator == scheme.separator
    assert f"must not contain {scheme.separator!r}" in str(caught.value)


# ---- RoomIdScheme.slugify ----


@pytest.mark.parametrize(
    "name, expected",
    [
        ("notes", "notes"),
        ("Q3 Marketing Plans!", "q3-marketing-plans"),
        ("  --Leading and trailing--  ", "leading-and-trailing"),
        ("Ünïcode Café", "unicode-cafe"),
        ("v1.2 / draft", "v1-2-draft"),
        ("a" * 70, "a" * room_ids.MAX_SEGMENT),
        # truncation lands on a separator: it is stripped, not kept
        ("a" * 63 + " b", "a" * 63),
    ],
)
def test_slugify(name, expected):
    result = SCHEME.slugify(name)

    assert result == expected
    assert SCHEME.is_segment(result)


def test_slugify_honors_max_segment():
    scheme = room_ids.RoomIdScheme(max_segment=5)

    result = scheme.slugify("abc defgh")

    assert result == "abc-d"


@pytest.mark.parametrize("name", ["", "   ", "!!!", "会议记录", "🙂"])
def test_slugify_rejects_names_yielding_nothing(name):
    with pytest.raises(room_ids.RoomIdError) as caught:
        SCHEME.slugify(name)

    assert type(caught.value) is room_ids.SlugEmpty
    assert caught.value.name == name
    assert "choose one explicitly" in str(caught.value)


# ---- RoomIdScheme.make_room_id ----


@pytest.mark.parametrize(
    "scheme, expected",
    [
        (SCHEME, f"owner_site.example{room_ids.DEFAULT_SEPARATOR}marketing"),
        (COLON, "owner_site.example:marketing"),
        (UNDERSCORES, "owner_site.example__marketing"),
    ],
    ids=["default", "colon", "underscores"],
)
def test_make_room_id(scheme, expected):
    owner = room_ids.RoomOwner(
        owner="owner_site.example",
        json_path='$[?$.email == "owner@site.example"]',
    )

    result = scheme.make_room_id(owner, "marketing")

    assert result == room_ids.RoomId(
        owner="owner_site.example", slug="marketing", scheme=scheme
    )
    assert str(result) == expected


def test_make_room_id_rejects_a_slug_carrying_the_separator():
    owner = _owner()

    with pytest.raises(room_ids.BadSlug) as caught:
        UNDERSCORES.make_room_id(owner, "q3__plans")

    assert caught.value.slug == "q3__plans"
    assert caught.value.separator == "__"


def test_make_room_id_rejects_a_bad_slug():
    owner = _owner()

    with pytest.raises(room_ids.BadSlug) as caught:
        SCHEME.make_room_id(owner, "../escape")

    assert caught.value.slug == "../escape"


@pytest.mark.parametrize(
    "scheme, owner",
    [(SCHEME, "bob-"), (SCHEME, "a--b"), (UNDERSCORES, "a__b"), (SCHEME, "")],
    ids=["bad-ending", "separator", "custom-separator", "empty"],
)
def test_make_room_id_rejects_an_owner_part_the_scheme_cannot_make(
    scheme, owner
):
    with pytest.raises(room_ids.BadOwnerPart) as caught:
        scheme.make_room_id(_owner(owner), "notes")

    assert caught.value.owner == owner
    assert f"must not contain {scheme.separator!r}" in str(caught.value)


# ---- RoomIdScheme.parse_room_id ----


@pytest.mark.parametrize(
    "scheme, room_id",
    [
        (SCHEME, f"alice{room_ids.DEFAULT_SEPARATOR}notes"),
        (COLON, "alice:notes"),
        (UNDERSCORES, "alice__notes"),
    ],
    ids=["default", "colon", "underscores"],
)
def test_parse_room_id(scheme, room_id):
    result = scheme.parse_room_id(room_id)

    assert result == room_ids.RoomId(
        owner="alice", slug="notes", scheme=scheme
    )


@pytest.mark.parametrize(
    "scheme, room_id",
    [
        (SCHEME, "notes"),  # an operator-curated (flat) id
        (SCHEME, "__provision__"),
        (SCHEME, "--notes"),
        (SCHEME, "alice--"),
        (SCHEME, "al/ice--notes"),
        (SCHEME, "alice--.notes"),
        (SCHEME, "alice--notes--extra"),
        (SCHEME, "bob---notes"),  # #143
        (SCHEME, "alice--" + "a" * (room_ids.MAX_SEGMENT + 1)),
        (SCHEME, "alice:notes"),  # the legacy form, without its separator
        (COLON, "notes"),
        (COLON, ":notes"),
        (COLON, "alice:"),
        (COLON, "alice:notes:extra"),
        (COLON, "al/ice:notes"),
        (COLON, "alice:.notes"),
        (COLON, "bob-:notes"),  # the old rule allowed it: see release notes
        (COLON, "alice:" + "a" * (room_ids.MAX_SEGMENT + 1)),
    ],
)
def test_parse_room_id_rejects(scheme, room_id):
    with pytest.raises(room_ids.RoomIdError) as caught:
        scheme.parse_room_id(room_id)

    assert type(caught.value) is room_ids.NotAProvisionedRoomId
    assert caught.value.room_id == room_id
    assert caught.value.separator == scheme.separator


# ---- round trip (#143) ----

_SEPARATORS = [
    room_ids.DEFAULT_SEPARATOR,
    ":",
    "__",
    "_",
    "~",
    "-_",
    ".",
    "_-",
]
_ALPHABET = ["a", "Z", "0", "-", "_", "."]
_PARTS = [
    "".join(chars)
    for length in (1, 2, 3)
    for chars in itertools.product(_ALPHABET, repeat=length)
]


@pytest.mark.parametrize("separator", _SEPARATORS)
def test_room_ids_round_trip(separator):
    """Every id a scheme makes, from owner and slug candidates drawn from
    the segment alphabet, splits back into the same parts."""
    scheme = room_ids.RoomIdScheme(separator=separator)
    made = []
    for owner_candidate, slug in itertools.product(_PARTS, _PARTS):
        try:
            owner = scheme.named_owner(owner_candidate, _RULE)
            made.append(scheme.make_room_id(owner, slug))
        except room_ids.RoomIdError:
            continue

    parsed = [scheme.parse_room_id(str(room_id)) for room_id in made]

    assert made  # the alphabet yields usable ids under every separator
    assert parsed == made


# ---- RoomId ----


def test_room_id():
    result = room_ids.RoomId(owner="alice", slug="notes")

    assert result.scheme is room_ids.DEFAULT_SCHEME
    assert result.separator == room_ids.DEFAULT_SEPARATOR
    assert str(result) == "alice--notes"
    assert repr(result) == "RoomId(owner='alice', slug='notes')"


def test_room_ids_under_different_schemes_differ():
    lowercase = room_ids.RoomId(owner="alice", slug="notes")

    result = room_ids.RoomId(
        owner="alice",
        slug="notes",
        scheme=room_ids.RoomIdScheme(lowercase_owner=False),
    )

    assert str(result) == str(lowercase)
    assert result != lowercase


@pytest.mark.parametrize(
    "args, kwargs",
    [((":",), {}), ((), {"separator": ":"})],
    ids=["positional", "keyword"],
)
def test_room_id_with_a_separator_is_deprecated(args, kwargs):
    with pytest.deprecated_call(match=r"RoomId\(separator=\.\.\.\)"):
        result = room_ids.RoomId("alice", "notes", *args, **kwargs)

    assert result == room_ids.RoomId("alice", "notes", COLON)


def test_room_id_refuses_a_scheme_and_a_separator():
    with pytest.raises(room_ids.SchemeConflict) as caught:
        room_ids.RoomId("alice", "notes", COLON, separator=":")

    assert caught.value.deprecated == "'separator'"
    assert isinstance(caught.value, TypeError)


# ---- deprecated module-level shims ----


@pytest.mark.parametrize(
    "shim, args, kwargs, expected",
    [
        (room_ids.is_segment, ("bob-",), {}, False),
        (room_ids.without_separator, ("a::b",), {"separator": ":"}, "a--b"),
        (
            room_ids.named_owner,
            ("Ops-", _GROUP_RULE),
            {"separator": ":", "lowercase": False},
            room_ids.RoomOwner(owner="Ops", json_path=_GROUP_RULE),
        ),
        (
            room_ids.owner_from_claims,
            ({"email": "Bob@Site"},),
            {
                "claim": "email",
                "flatten_value": True,
                "lowercase_value": False,
                "separator": "__",
            },
            room_ids.RoomOwner(
                owner="Bob_Site", json_path='$[?$.email == "Bob@Site"]'
            ),
        ),
        (room_ids.validate_slug, ("q3--plans",), {"separator": ":"}, None),
        (room_ids.slugify, ("Q3 Plans",), {}, "q3-plans"),
        (
            room_ids.make_room_id,
            (_owner(), "notes"),
            {"separator": ":"},
            room_ids.RoomId("alice", "notes", COLON),
        ),
        (
            room_ids.parse_room_id,
            ("alice:notes",),
            {"separator": ":"},
            room_ids.RoomId("alice", "notes", COLON),
        ),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_shims_are_deprecated(shim, args, kwargs, expected):
    match = (
        rf"room_ids\.{shim.__name__} is deprecated: use "
        rf"RoomIdScheme\.{shim.__name__} instead \(it will not be removed "
        r"until after v0\.16\)"
    )

    with pytest.deprecated_call(match=match):
        result = shim(*args, **kwargs)

    assert result == expected
