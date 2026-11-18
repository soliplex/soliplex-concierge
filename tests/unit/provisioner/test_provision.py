"""Unit tests for ``soliplex_concierge.provisioner.provision``.

The adapters are fakes (``fake_parts``), sharing a script that records
each call, so a test can check the steps' order as well as their outcome.
"""

import datetime
from unittest import mock

import fake_parts
import pytest

from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import provision
from soliplex_concierge.provisioner import room_ids

CLAIMS = {"preferred_username": "alice", "email": "alice@example.com"}
NOW = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.UTC)
ROOM = "alice--q3-plans"

# The calls of a clean admission, in order.
ADMISSION = [
    "backend.check_access",
    "store.lock",
    "store.prepare",
    "store.has_room",
    "store.clashes",
    "registrar.clashes",
    "uploads.clashes",
    "authz.clashes",
    "tracker.file",
    "store.unlock",
]
CHECK_FREE = [
    "store.has_room",
    "store.clashes",
    "registrar.clashes",
    "uploads.clashes",
    "authz.clashes",
]
CREATION_STEPS = [
    "store.add_room",
    "uploads.create",
    "registrar.register",
    "authz.grant_owner",
    "authz.audit",
    "store.commit",
]


def _request(provisioner, **kwargs):
    return provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner, now=NOW, **kwargs
    )


def _admitted(script, provisioner):
    """An admission, as admit_room makes it; the script's journal reset."""
    admitted = provision.admit_room(_request(provisioner), provisioner)
    script.journal.clear()
    return admitted


# ---- Provisioner ----


def test_provisioner_defaults():
    result = fake_parts.make_provisioner(fake_parts.Script())

    assert result.owner_claim == room_ids.DEFAULT_OWNER_CLAIM
    assert result.scheme is room_ids.DEFAULT_SCHEME


def test_provisioner_with_a_scheme():
    scheme = room_ids.RoomIdScheme(separator=":", lowercase_owner=False)

    result = fake_parts.make_provisioner(fake_parts.Script(), scheme=scheme)

    assert result.scheme is scheme


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"room_id_separator": ":"}, room_ids.RoomIdScheme(separator=":")),
        (
            {"flatten_owner": True, "lowercase_owner": False},
            room_ids.RoomIdScheme(flatten_owner=True, lowercase_owner=False),
        ),
    ],
    ids=["separator", "owner-settings"],
)
def test_provisioner_legacy_keywords_are_deprecated(kwargs, expected):
    with pytest.deprecated_call(match=r"Provisioner\(room_id_separator="):
        result = fake_parts.make_provisioner(fake_parts.Script(), **kwargs)

    assert result.scheme == expected


@pytest.mark.parametrize("separator", ["", "-", "/", "a b", "X"])
def test_provisioner_legacy_keyword_rejects_a_bad_separator(separator):
    with (
        pytest.deprecated_call(),
        pytest.raises(room_ids.BadSeparator) as caught,
    ):
        fake_parts.make_provisioner(
            fake_parts.Script(), room_id_separator=separator
        )

    assert caught.value.separator == separator


def test_provisioner_refuses_a_scheme_and_legacy_keywords():
    with pytest.raises(room_ids.SchemeConflict):
        fake_parts.make_provisioner(
            fake_parts.Script(),
            scheme=room_ids.DEFAULT_SCHEME,
            room_id_separator=":",
        )


@pytest.mark.parametrize(
    "attribute, expected",
    [
        ("room_id_separator", ":"),
        ("flatten_owner", True),
        ("lowercase_owner", False),
    ],
)
def test_provisioner_legacy_attributes_are_deprecated(attribute, expected):
    scheme = room_ids.RoomIdScheme(
        separator=":", flatten_owner=True, lowercase_owner=False
    )
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), scheme=scheme
    )

    with pytest.deprecated_call(match=rf"Provisioner\.{attribute}"):
        result = getattr(provisioner, attribute)

    assert result == expected


# ---- RequestRefused / check_request (#149) ----


@pytest.mark.parametrize(
    "status, retry_after",
    [(400, None), (409, None), (429, None), (429, 60)],
)
def test_request_refused(status, retry_after):
    result = provision.RequestRefused(
        status, "too many", retry_after=retry_after
    )

    assert (result.status, result.detail, result.retry_after) == (
        status,
        "too many",
        retry_after,
    )
    assert str(result) == "too many"


def test_provisioner_has_no_request_check_by_default():
    result = fake_parts.make_provisioner(fake_parts.Script())

    assert result.check_request is None


def test_check_request_without_a_check():
    provisioner = fake_parts.make_provisioner(fake_parts.Script())

    result = provision.check_request(
        provisioner, CLAIMS, _request(provisioner)
    )

    assert result is None


def test_check_request_calls_the_check():
    check = mock.Mock(return_value=None)
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), check_request=check
    )
    request = _request(provisioner)

    provision.check_request(provisioner, CLAIMS, request)

    check.assert_called_once_with(CLAIMS, request)


def test_check_request_propagates_a_refusal():
    refusal = provision.RequestRefused(429, "slow down", retry_after=60)
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), check_request=mock.Mock(side_effect=refusal)
    )

    with pytest.raises(provision.RequestRefused) as caught:
        provision.check_request(provisioner, CLAIMS, _request(provisioner))

    assert caught.value is refusal


# ---- plan_room ----


def test_plan_room():
    provisioner = fake_parts.make_provisioner(fake_parts.Script())

    result = provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner, now=NOW
    )

    assert result == interfaces.RoomRequest(
        requester_claims=CLAIMS,
        owner=room_ids.RoomOwner(
            owner="alice", json_path='$[?$.preferred_username == "alice"]'
        ),
        room_id=room_ids.RoomId(owner="alice", slug="q3-plans"),
        name="Q3 Plans",
        description="Q3 Plans",
        requested_at=NOW,
    )
    assert result.room == ROOM


def test_plan_room_with_a_slug_and_a_description():
    provisioner = fake_parts.make_provisioner(fake_parts.Script())

    result = provision.plan_room(
        CLAIMS,
        name="Q3 Plans",
        provisioner=provisioner,
        description="Plans for Q3",
        slug="plans",
        now=NOW,
    )

    assert result.room == "alice--plans"
    assert result.description == "Plans for Q3"


def test_plan_room_flattens_the_owner_claim():
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(),
        owner_claim="email",
        scheme=room_ids.RoomIdScheme(flatten_owner=True),
    )

    result = provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner, now=NOW
    )

    assert result.room == "alice_example.com--q3-plans"
    assert result.owner.json_path == '$[?$.email == "alice@example.com"]'


def test_plan_room_with_a_room_id_separator():
    scheme = room_ids.RoomIdScheme(separator=":")
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), scheme=scheme
    )

    result = provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner, now=NOW
    )

    assert result.room == "alice:q3-plans"
    assert result.room_id == room_ids.RoomId(
        owner="alice", slug="q3-plans", scheme=scheme
    )


@pytest.mark.parametrize(
    "separator, username, expected",
    [
        ("__", "jo__smith", "jo-smith__q3-plans"),
        ("--", "bob-", "bob--q3-plans"),
        ("__", "bob_", "bob__q3-plans"),
    ],
    ids=["separator-inside", "trailing-dash", "trailing-underscore"],
)
def test_plan_room_normalizes_the_owner(separator, username, expected):
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(),
        scheme=room_ids.RoomIdScheme(separator=separator),
    )
    claims = {"preferred_username": username}

    result = provision.plan_room(
        claims, name="Q3 Plans", provisioner=provisioner, now=NOW
    )

    assert result.room == expected
    assert result.owner.json_path == (
        f'$[?$.preferred_username == "{username}"]'
    )


@pytest.mark.parametrize(
    "scheme, expected",
    [
        (room_ids.DEFAULT_SCHEME, "alice--q3-plans"),
        (room_ids.RoomIdScheme(lowercase_owner=False), "Alice--q3-plans"),
    ],
    ids=["default", "keep-case"],
)
def test_plan_room_lowercases_the_owner(scheme, expected):
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), scheme=scheme
    )
    claims = {"preferred_username": "Alice"}

    result = provision.plan_room(
        claims, name="Q3 Plans", provisioner=provisioner, now=NOW
    )

    assert result.room == expected
    assert result.owner.json_path == '$[?$.preferred_username == "Alice"]'


def test_plan_room_refuses_a_slug_carrying_the_separator():
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), scheme=room_ids.RoomIdScheme(separator="__")
    )

    with pytest.raises(room_ids.BadSlug) as caught:
        provision.plan_room(
            CLAIMS,
            name="Q3 Plans",
            provisioner=provisioner,
            slug="q3__plans",
            now=NOW,
        )

    assert caught.value.separator == "__"


def test_plan_room_for_a_named_owner():
    provisioner = fake_parts.make_provisioner(fake_parts.Script())
    staff = provisioner.scheme.named_owner("staff", "$[?'staff' in @.groups]")

    result = provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner, owner=staff, now=NOW
    )

    assert result.room == "staff--q3-plans"
    assert result.owner == staff
    assert result.requester_claims == CLAIMS


@pytest.mark.parametrize(
    "scheme, owner, expected",
    [
        (
            room_ids.RoomIdScheme(separator="."),
            "night.shift_crew",
            "night-shift_crew.q3-plans",
        ),
        (
            room_ids.RoomIdScheme(lowercase_owner=False),
            "Night--Crew-",
            "Night_Crew--q3-plans",
        ),
        (room_ids.DEFAULT_SCHEME, "Night--Crew-", "night_crew--q3-plans"),
    ],
    ids=["separator", "keep-case", "lowercase"],
)
def test_plan_room_normalizes_a_named_owner_under_its_scheme(
    scheme, owner, expected
):
    """An owner named under other rules still gets the provisioner's (#156)."""
    provisioner = fake_parts.make_provisioner(
        fake_parts.Script(), scheme=scheme
    )
    named = room_ids.RoomOwner(owner=owner, json_path="$[?is_crew($)]")

    result = provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner, owner=named, now=NOW
    )

    assert result.room == expected
    assert scheme.parse_room_id(result.room) == result.room_id
    assert result.owner.json_path == "$[?is_crew($)]"


def test_plan_room_defaults_to_now():
    provisioner = fake_parts.make_provisioner(fake_parts.Script())
    before = datetime.datetime.now(datetime.UTC)

    result = provision.plan_room(
        CLAIMS, name="Q3 Plans", provisioner=provisioner
    )

    assert before <= result.requested_at <= datetime.datetime.now(datetime.UTC)


@pytest.mark.parametrize(
    "claims, name, slug, error",
    [
        (CLAIMS, "", None, provision.BadRoomName),
        (CLAIMS, "   ", None, provision.BadRoomName),
        (CLAIMS, "!!!", None, room_ids.SlugEmpty),
        (CLAIMS, "Q3 Plans", "../escape", room_ids.BadSlug),
        ({"sub": "alice-sub"}, "Q3", None, room_ids.OwnerClaimMissing),
    ],
    ids=["empty-name", "blank-name", "no-slug", "bad-slug", "no-owner"],
)
def test_plan_room_refuses(claims, name, slug, error):
    provisioner = fake_parts.make_provisioner(fake_parts.Script())

    with pytest.raises(room_ids.RoomIdError) as caught:
        provision.plan_room(
            claims, name=name, provisioner=provisioner, slug=slug
        )

    assert type(caught.value) is error


# ---- admit_room ----


def test_admit_room():
    script = fake_parts.Script()
    provisioner = fake_parts.make_provisioner(script)
    request = _request(provisioner)

    result = provision.admit_room(request, provisioner)

    assert result == provision.Admitted(
        request=request, issue=fake_parts.FakeIssue()
    )
    assert script.journal == ADMISSION


def test_admit_room_without_a_tracker():
    script = fake_parts.Script()
    provisioner = fake_parts.make_provisioner(script, tracker=False)
    request = _request(provisioner)

    result = provision.admit_room(request, provisioner)

    assert result == provision.Admitted(request=request)
    assert script.journal == [
        name for name in ADMISSION if name != "tracker.file"
    ]


def test_admit_room_refused_by_the_backend():
    denied = RuntimeError("the backend refused the provisioner")
    script = fake_parts.Script(failures={"backend.check_access": denied})
    provisioner = fake_parts.make_provisioner(script)
    request = _request(provisioner)

    with pytest.raises(RuntimeError) as caught:
        provision.admit_room(request, provisioner)

    assert caught.value is denied
    assert script.journal == ["backend.check_access"]


def test_admit_room_refuses_a_room_the_store_holds():
    script = fake_parts.Script(has_room=True)
    provisioner = fake_parts.make_provisioner(script)
    request = _request(provisioner)

    with pytest.raises(provision.RoomExists) as caught:
        provision.admit_room(request, provisioner)

    assert caught.value.room_id == ROOM
    assert "already exists; choose another name" in str(caught.value)
    assert script.journal == [
        "backend.check_access",
        "store.lock",
        "store.prepare",
        "store.has_room",
        "store.unlock",
    ]


def test_admit_room_refuses_clashes():
    script = fake_parts.Script(
        clashes={
            "authz": ["an authz policy"],
            "registrar": ["a database", "an ingester entry"],
            "store": ["a fragment"],
            "uploads": ["3 uploads"],
        }
    )
    provisioner = fake_parts.make_provisioner(script)
    request = _request(provisioner)

    with pytest.raises(provision.ResourcesExist) as caught:
        provision.admit_room(request, provisioner)

    assert caught.value.found == (
        "a fragment",
        "a database",
        "an ingester entry",
        "3 uploads",
        "an authz policy",
    )
    assert (
        "resources named for it already exist: a fragment; a database"
        in str(caught.value)
    )
    assert "tracker.file" not in script.journal
    assert script.journal[-1] == "store.unlock"


def test_admit_room_issue_filing_fails():
    error = RuntimeError("Gitea is down")
    script = fake_parts.Script(failures={"tracker.file": error})
    provisioner = fake_parts.make_provisioner(script)
    request = _request(provisioner)

    with pytest.raises(RuntimeError) as caught:
        provision.admit_room(request, provisioner)

    assert caught.value is error
    assert script.journal[-1] == "store.unlock"


# ---- create_room ----


def test_create_room():
    script = fake_parts.Script()
    provisioner = fake_parts.make_provisioner(script)
    admitted = _admitted(script, provisioner)

    result = provision.create_room(admitted, provisioner)

    assert result == interfaces.Provisioned(
        request=admitted.request,
        commit=fake_parts.COMMIT,
        issue=fake_parts.FakeIssue(),
    )
    assert script.journal == [
        "store.lock",
        *CHECK_FREE,
        *CREATION_STEPS,
        "store.publish",
        "store.unlock",
        "backend.reload_rooms",
        "tracker.resolve",
    ]
    assert script.resolved == [result]


def test_create_room_without_a_tracker():
    script = fake_parts.Script()
    provisioner = fake_parts.make_provisioner(script, tracker=False)
    admitted = _admitted(script, provisioner)

    result = provision.create_room(admitted, provisioner)

    assert result.issue is None
    assert script.journal[-1] == "backend.reload_rooms"


@pytest.mark.parametrize(
    "tracker, report_fails",
    [(True, False), (True, True), (False, False)],
    ids=["reported", "report-fails", "no-tracker"],
)
def test_create_room_refuses_on_its_second_check(tracker, report_fails):
    failures = {}
    if report_fails:
        failures["tracker.report_failure"] = RuntimeError("Gitea is down")
    script = fake_parts.Script(failures=failures)
    provisioner = fake_parts.make_provisioner(script, tracker=tracker)
    admitted = _admitted(script, provisioner)
    script.has_room = True  # another run created it in the meantime

    with pytest.raises(provision.RoomExists) as caught:
        provision.create_room(admitted, provisioner)

    assert script.reported == ([caught.value] if tracker else [])
    assert "store.add_room" not in script.journal
    assert script.journal[-1] == "store.unlock"


@pytest.mark.parametrize(
    "failing, undone",
    [
        ("store.add_room", []),
        ("uploads.create", ["store.add_room"]),
        ("registrar.register", ["uploads.create", "store.add_room"]),
        (
            "authz.grant_owner",
            ["registrar.register", "uploads.create", "store.add_room"],
        ),
        (
            "authz.audit",
            [
                "authz.grant_owner",
                "registrar.register",
                "uploads.create",
                "store.add_room",
            ],
        ),
        (
            "store.commit",
            [
                "authz.grant_owner",
                "registrar.register",
                "uploads.create",
                "store.add_room",
            ],
        ),
    ],
)
def test_create_room_rolls_back(failing, undone):
    error = RuntimeError(f"{failing} broke")
    script = fake_parts.Script(failures={failing: error})
    provisioner = fake_parts.make_provisioner(script)
    admitted = _admitted(script, provisioner)

    with pytest.raises(provision.ProvisioningFailed) as caught:
        provision.create_room(admitted, provisioner)

    failed = caught.value
    assert failed.error is error
    assert failed.__cause__ is error
    assert failed.rollback_failures == ()
    assert str(failed) == (
        f"provisioning room {ROOM!r} failed: {failing} broke; rolled back"
    )
    steps = CREATION_STEPS[: CREATION_STEPS.index(failing) + 1]
    assert script.journal == [
        "store.lock",
        *CHECK_FREE,
        *steps,
        *(f"undo {name}" for name in undone),
        "tracker.report_failure",
        "store.unlock",
    ]
    assert script.reported == [failed]


def test_create_room_reports_undo_failures():
    script = fake_parts.Script(
        failures={"authz.audit": RuntimeError("audit failed")},
        undo_failures={"registrar.register": RuntimeError("ingester down")},
    )
    provisioner = fake_parts.make_provisioner(script)
    admitted = _admitted(script, provisioner)

    with pytest.raises(provision.ProvisioningFailed) as caught:
        provision.create_room(admitted, provisioner)

    assert caught.value.rollback_failures == (
        "undo registrar.register (ingester down)",
    )
    assert str(caught.value).endswith(
        "audit failed; rolling back, these steps failed: "
        "undo registrar.register (ingester down)"
    )
    assert "undo store.add_room" in script.journal


def test_create_room_keeps_uploads_it_did_not_create():
    script = fake_parts.Script(
        failures={"registrar.register": RuntimeError("no database")},
        uploads_exist=True,
    )
    provisioner = fake_parts.make_provisioner(script)
    admitted = _admitted(script, provisioner)

    with pytest.raises(provision.ProvisioningFailed):
        provision.create_room(admitted, provisioner)

    assert "undo uploads.create" not in script.journal
    assert "undo store.add_room" in script.journal


def test_create_room_without_a_tracker_rolls_back_silently():
    script = fake_parts.Script(failures={"store.commit": RuntimeError("x")})
    provisioner = fake_parts.make_provisioner(script, tracker=False)
    admitted = _admitted(script, provisioner)

    with pytest.raises(provision.ProvisioningFailed):
        provision.create_room(admitted, provisioner)

    assert script.reported == []


def test_create_room_reports_what_remains_to_do():
    script = fake_parts.Script(
        unfinished={
            "store.publish": ["push the rooms repository"],
            "backend.reload_rooms": ["reload the backend's rooms"],
            "tracker.resolve": ["close the issue"],
        }
    )
    provisioner = fake_parts.make_provisioner(script)
    admitted = _admitted(script, provisioner)

    result = provision.create_room(admitted, provisioner)

    assert result.unfinished == (
        "push the rooms repository",
        "reload the backend's rooms",
        "close the issue",
    )
    [resolved] = script.resolved
    assert resolved.unfinished == (
        "push the rooms repository",
        "reload the backend's rooms",
    )


def test_create_room_reports_failed_steps_after_the_commit():
    script = fake_parts.Script(
        failures={
            "store.publish": RuntimeError("push refused"),
            "backend.reload_rooms": RuntimeError("backend down"),
            "tracker.resolve": RuntimeError("Gitea down"),
        }
    )
    provisioner = fake_parts.make_provisioner(script)
    admitted = _admitted(script, provisioner)

    result = provision.create_room(admitted, provisioner)

    assert result.unfinished == ("push refused", "backend down", "Gitea down")
    assert result.commit == fake_parts.COMMIT
    assert "store.unlock" in script.journal


# ---- provision_room ----


def test_provision_room():
    script = fake_parts.Script()
    provisioner = fake_parts.make_provisioner(script)
    staff = provisioner.scheme.named_owner("staff", "$[?'staff' in @.groups]")

    result = provision.provision_room(
        CLAIMS,
        name="Q3 Plans",
        provisioner=provisioner,
        description="Plans for Q3",
        slug="plans",
        owner=staff,
        now=NOW,
    )

    assert result.request.room == "staff--plans"
    assert result.request.requester_claims == CLAIMS
    assert result.request.description == "Plans for Q3"
    assert result.request.requested_at == NOW
    assert script.journal[: len(ADMISSION)] == ADMISSION
    assert script.journal[-1] == "tracker.resolve"
