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


# ---- named_owner ----


def test_named_owner():
    result = room_ids.named_owner("staff", "$[?'staff' in @.groups]")

    assert result == room_ids.RoomOwner(
        owner="staff", json_path="$[?'staff' in @.groups]"
    )


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


@pytest.mark.parametrize("slug", _BAD_SEGMENTS)
def test_validate_slug_rejects(slug):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.validate_slug(slug)

    assert type(caught.value) is room_ids.BadSlug
    assert caught.value.slug == slug


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


def test_make_room_id():
    owner = room_ids.RoomOwner(
        owner="bharney_sitename.example",
        json_path='$[?$.email == "bharney@sitename.example"]',
    )

    result = room_ids.make_room_id(owner, "marketing")

    assert result == room_ids.RoomId(
        owner="bharney_sitename.example", slug="marketing"
    )
    assert str(result) == "bharney_sitename.example:marketing"


def test_make_room_id_rejects_a_bad_slug():
    owner = _owner()

    with pytest.raises(room_ids.BadSlug) as caught:
        room_ids.make_room_id(owner, "../escape")

    assert caught.value.slug == "../escape"


# ---- parse_room_id ----


def test_parse_room_id():
    result = room_ids.parse_room_id("alice:notes")

    assert result == room_ids.RoomId(owner="alice", slug="notes")


@pytest.mark.parametrize(
    "room_id",
    [
        "notes",  # an operator-curated (flat) id
        "__provision__",
        ":notes",
        "alice:",
        "alice:notes:extra",
        "al/ice:notes",
        "alice:.notes",
        "alice:" + "a" * (room_ids.MAX_SEGMENT + 1),
    ],
)
def test_parse_room_id_rejects(room_id):
    with pytest.raises(room_ids.RoomIdError) as caught:
        room_ids.parse_room_id(room_id)

    assert type(caught.value) is room_ids.NotAProvisionedRoomId
    assert caught.value.room_id == room_id
