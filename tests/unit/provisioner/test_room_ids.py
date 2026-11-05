"""Unit tests for ``soliplex_concierge.provisioner.room_ids``.

Each test is laid out in three blank-line-separated phases -- setup, then the
single call under test (the "act"), then the assertions -- and performs that
act exactly once (cases that would repeat it are parametrized or split).
"""

from __future__ import annotations

import pytest

from soliplex_concierge.provisioner import access_rules
from soliplex_concierge.provisioner import room_ids

# Values that are not one safe path segment, or that carry the separator.
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


def _owner(owner="alice"):
    return room_ids.RoomOwner(
        owner=owner, json_path=f'$[?$.preferred_username == "{owner}"]'
    )


# ---- is_segment ----


@pytest.mark.parametrize(
    "value, expected",
    [
        ("alice", True),
        ("a" * room_ids.MAX_SEGMENT, True),
        *[(bad, False) for bad in _BAD_SEGMENTS],
    ],
)
def test_is_segment(value, expected):
    result = room_ids.is_segment(value)

    assert result is expected


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


# ---- validate_separator ----


@pytest.mark.parametrize(
    "separator",
    [room_ids.DEFAULT_SEPARATOR, "--", ":", "__", "_", "~", "-_", "a--b", "X"],
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
        ("\u2192", "it contains '\u2192'"),
        ("-", "slugs derived from room names can contain it"),
        ("x", "slugs derived from room names can contain it"),
        ("a-b", "slugs derived from room names can contain it"),
        ("-a-", "slugs derived from room names can contain it"),
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


# ---- without_separator ----


@pytest.mark.parametrize(
    "value, kw, expected",
    [
        ("alice", {}, "alice"),
        ("a--b", {}, "a_b"),
        ("a---b", {}, "a_-b"),
        ("a----b", {}, "a__b"),
        ("a:b", {"separator": ":"}, "a-b"),
        ("a::b", {"separator": ":"}, "a--b"),
        ("a__b", {"separator": "__"}, "a-b"),
        ("a___b", {"separator": "__"}, "a-_b"),
        ("a-_-_b", {"separator": "-_"}, "a__b"),
        ("a--b", {"separator": ""}, "a--b"),
    ],
)
def test_without_separator(value, kw, expected):
    result = room_ids.without_separator(value, **kw)

    assert result == expected


# ---- named_owner ----


def test_named_owner():
    result = room_ids.named_owner("staff", "$[?'staff' in @.groups]")

    assert result == room_ids.RoomOwner(
        owner="staff", json_path="$[?'staff' in @.groups]"
    )


@pytest.mark.parametrize(
    "owner, kw, expected",
    [
        ("night--shift", {}, "night_shift"),
        ("night:shift", {"separator": ":"}, "night-shift"),
        ("Night--Shift", {}, "night_shift"),
        ("Night--Shift", {"lowercase": False}, "Night_Shift"),
    ],
)
def test_named_owner_normalizes(owner, kw, expected):
    result = room_ids.named_owner(owner, "$[?'staff' in @.groups]", **kw)

    assert result.owner == expected


@pytest.mark.parametrize("owner", _BAD_SEGMENTS)
def test_named_owner_rejects_a_bad_owner_part(owner):
    with pytest.raises(room_ids.BadOwnerPart) as caught:
        room_ids.named_owner(owner, "$[?'staff' in @.groups]")

    assert caught.value.owner == owner


@pytest.mark.parametrize("json_path", ["", "   "])
def test_named_owner_needs_an_access_rule(json_path):
    with pytest.raises(room_ids.NoAccessRule) as caught:
        room_ids.named_owner("staff", json_path)

    assert caught.value.owner == "staff"
    assert "needs an access rule" in str(caught.value)


# ---- owner_from_claims ----


@pytest.mark.parametrize(
    "claims, kwargs, expected_value, expected_owner",
    [
        ({"preferred_username": "alice"}, {}, "alice", "alice"),
        (
            {"preferred_username": "12345678901234567"},
            {},
            "12345678901234567",
            "12345678901234567",
        ),
        (
            {"email": "bharney@sitename.example"},
            {"claim": "email", "flatten_value": True},
            "bharney@sitename.example",
            "bharney_sitename.example",
        ),
        ({"preferred_username": "jo--smith"}, {}, "jo--smith", "jo_smith"),
        ({"preferred_username": "Alice"}, {}, "Alice", "alice"),
        (
            {"preferred_username": "Alice"},
            {"lowercase_value": False},
            "Alice",
            "Alice",
        ),
        (
            {"email": "Jo.Smith@Example.COM"},
            {"claim": "email", "flatten_value": True},
            "Jo.Smith@Example.COM",
            "jo.smith_example.com",
        ),
        (
            {"preferred_username": "jo:smith"},
            {"separator": ":"},
            "jo:smith",
            "jo-smith",
        ),
        (
            {"email": "jo--smith@sitename.example"},
            {"claim": "email", "flatten_value": True},
            "jo--smith@sitename.example",
            "jo_smith_sitename.example",
        ),
    ],
)
def test_owner_from_claims(claims, kwargs, expected_value, expected_owner):
    claim = kwargs.get("claim", room_ids.DEFAULT_OWNER_CLAIM)

    result = room_ids.owner_from_claims(claims, **kwargs)

    assert result == room_ids.RoomOwner(
        owner=expected_owner,
        json_path=f'$[?$.{claim} == "{expected_value}"]',
    )


def test_owner_from_claims_rejects_a_bad_claim():
    claims = {"a.b": "alice"}

    with pytest.raises(access_rules.BadOwnerClaim) as caught:
        room_ids.owner_from_claims(claims, claim="a.b")

    assert caught.value.claim == "a.b"


def test_owner_from_claims_requires_the_claim():
    claims = {"email": "alice@example.com"}

    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.owner_from_claims(claims)

    assert type(caught.value) is room_ids.OwnerClaimMissing
    assert caught.value.claim == "preferred_username"
    assert "'preferred_username'" in str(caught.value)


@pytest.mark.parametrize(
    "claims, kwargs",
    [
        ({"preferred_username": 12345}, {}),
        ({"preferred_username": None}, {}),
        ({"email": "alice@example.com"}, {"claim": "email"}),
        *[({"preferred_username": bad}, {}) for bad in _BAD_SEGMENTS],
    ],
)
def test_owner_from_claims_rejects_unusable_values(claims, kwargs):
    claim = kwargs.get("claim", room_ids.DEFAULT_OWNER_CLAIM)

    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.owner_from_claims(claims, **kwargs)

    assert type(caught.value) is room_ids.BadOwner
    assert caught.value.claim == claim
    assert caught.value.value == claims[claim]
    assert "cannot own rooms" in str(caught.value)


# ---- validate_slug ----


@pytest.mark.parametrize(
    "slug",
    ["notes", "Q3-plans", "v1.2_draft", "7", "a" * room_ids.MAX_SEGMENT],
)
def test_validate_slug_accepts(slug):
    result = room_ids.validate_slug(slug)

    assert result is None


@pytest.mark.parametrize(
    "slug, kw",
    [
        ("q3--plans", {"separator": ":"}),
        ("q3__plans", {"separator": "--"}),
        ("q3plans", {"separator": ""}),
    ],
)
def test_validate_slug_accepts_with_a_separator(slug, kw):
    result = room_ids.validate_slug(slug, **kw)

    assert result is None


@pytest.mark.parametrize("slug", _BAD_SEGMENTS)
def test_validate_slug_rejects(slug):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.validate_slug(slug)

    assert type(caught.value) is room_ids.BadSlug
    assert caught.value.slug == slug


@pytest.mark.parametrize(
    "slug, kw, separator",
    [
        ("q3--plans", {}, room_ids.DEFAULT_SEPARATOR),
        ("notes--extra", {"separator": "--"}, "--"),
        ("q3__plans", {"separator": "__"}, "__"),
    ],
)
def test_validate_slug_rejects_the_separator(slug, kw, separator):
    with pytest.raises(room_ids.BadSlug) as caught:
        room_ids.validate_slug(slug, **kw)

    assert caught.value.slug == slug
    assert caught.value.separator == separator
    assert f"must not contain {separator!r}" in str(caught.value)


# ---- slugify ----


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
    result = room_ids.slugify(name)

    assert result == expected
    assert room_ids.is_segment(result)


@pytest.mark.parametrize("name", ["", "   ", "!!!", "会议记录", "🙂"])
def test_slugify_rejects_names_yielding_nothing(name):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.slugify(name)

    assert type(caught.value) is room_ids.SlugEmpty
    assert caught.value.name == name
    assert "choose one explicitly" in str(caught.value)


# ---- make_room_id ----


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({}, f"owner_site.example{room_ids.DEFAULT_SEPARATOR}marketing"),
        ({"separator": ":"}, "owner_site.example:marketing"),
        ({"separator": "--"}, "owner_site.example--marketing"),
    ],
    ids=["default", "colon", "double-dash"],
)
def test_make_room_id(kw, expected):
    owner = room_ids.RoomOwner(
        owner="owner_site.example",
        json_path='$[?$.email == "owner@site.example"]',
    )

    result = room_ids.make_room_id(owner, "marketing", **kw)

    assert result == room_ids.RoomId(
        owner="owner_site.example", slug="marketing", **kw
    )
    assert str(result) == expected


def test_make_room_id_rejects_a_slug_carrying_the_separator():
    owner = _owner()

    with pytest.raises(room_ids.BadSlug) as caught:
        room_ids.make_room_id(owner, "q3__plans", separator="__")

    assert caught.value.slug == "q3__plans"
    assert caught.value.separator == "__"


def test_make_room_id_rejects_a_bad_slug():
    owner = _owner()

    with pytest.raises(room_ids.BadSlug) as caught:
        room_ids.make_room_id(owner, "../escape")

    assert caught.value.slug == "../escape"


# ---- parse_room_id ----


@pytest.mark.parametrize(
    "room_id, kw, separator",
    [
        (
            f"alice{room_ids.DEFAULT_SEPARATOR}notes",
            {},
            room_ids.DEFAULT_SEPARATOR,
        ),
        ("alice:notes", {"separator": ":"}, ":"),
        ("alice--notes", {"separator": "--"}, "--"),
    ],
    ids=["default", "colon", "double-dash"],
)
def test_parse_room_id(room_id, kw, separator):
    result = room_ids.parse_room_id(room_id, **kw)

    assert result == room_ids.RoomId(
        owner="alice", slug="notes", separator=separator
    )


_COLON = {"separator": ":"}


@pytest.mark.parametrize(
    "room_id, kw",
    [
        ("notes", {}),  # an operator-curated (flat) id
        ("__provision__", {}),
        ("--notes", {}),
        ("alice--", {}),
        ("al/ice--notes", {}),
        ("alice--.notes", {}),
        ("alice--" + "a" * (room_ids.MAX_SEGMENT + 1), {}),
        ("alice:notes", {}),  # the legacy form, without its separator
        ("notes", _COLON),
        (":notes", _COLON),
        ("alice:", _COLON),
        ("alice:notes:extra", _COLON),
        ("al/ice:notes", _COLON),
        ("alice:.notes", _COLON),
        ("alice:" + "a" * (room_ids.MAX_SEGMENT + 1), _COLON),
    ],
)
def test_parse_room_id_rejects(room_id, kw):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.parse_room_id(room_id, **kw)

    assert type(caught.value) is room_ids.NotAProvisionedRoomId
    assert caught.value.room_id == room_id
    assert caught.value.separator == kw.get(
        "separator", room_ids.DEFAULT_SEPARATOR
    )
