import pydantic
import pytest

from soliplex_concierge.provisioner import api


@pytest.mark.parametrize(
    "room_id, expected",
    [
        ("phreddy--q3-plans", "/rooms/phreddy--q3-plans"),
        ("phreddy:q3-plans", "/rooms/phreddy:q3-plans"),
        ("a:b/../ok", "/rooms/a:b%2F..%2Fok"),
        ("a b:c?d#e", "/rooms/a%20b:c%3Fd%23e"),
    ],
)
def test_room_path(room_id, expected):
    result = api.room_path(room_id)

    assert result == expected


def test_room_request_forbids_unknown_fields():
    with pytest.raises(pydantic.ValidationError):
        api.RoomRequest(name="Q3 Plans", colour="blue")


def test_room_request_defaults():
    result = api.RoomRequest(name="Q3 Plans")

    assert result.model_dump() == {
        "name": "Q3 Plans",
        "description": None,
        "slug": None,
        "owner": None,
    }


@pytest.mark.parametrize(
    "owner",
    [
        {"owner": "bharney", "claim": "email", "value": "b@example.com"},
        {"owner": "foremen", "predicate": "is_foreman"},
    ],
    ids=["claim", "predicate"],
)
def test_room_request_with_an_owner(owner):
    result = api.RoomRequest.model_validate(
        {"name": "Staff Notes", "owner": owner}
    )

    assert result.owner == api.RoomOwner(**owner)


@pytest.mark.parametrize(
    "owner",
    [
        {"owner": "x"},
        {"owner": "x", "claim": "email"},
        {"owner": "x", "value": "b@example.com"},
        {"owner": "x", "claim": "email", "value": "b", "predicate": "p"},
        {"owner": "x", "json_path": "$"},
    ],
    ids=["no-form", "claim-only", "value-only", "both-forms", "raw-rule"],
)
def test_room_owner_needs_exactly_one_form(owner):
    with pytest.raises(pydantic.ValidationError):
        api.RoomOwner(**owner)


def test_room_owner_reports_its_forms():
    with pytest.raises(pydantic.ValidationError) as caught:
        api.RoomOwner(owner="x", claim="email", predicate="is_staff")

    [error] = caught.value.errors()
    invalid = error["ctx"]["error"]
    assert type(invalid) is api.OwnerFormInvalid
    assert (invalid.claim, invalid.value, invalid.predicate) == (
        "email",
        None,
        "is_staff",
    )
    assert "predicate='is_staff'" in str(invalid)


def test_request_state_defaults():
    result = api.RequestState.model_validate_json(
        '{"room_id": "phreddy:q3-plans", "state": "live"}'
    )

    assert result == api.RequestState(
        room_id="phreddy:q3-plans",
        state=api.State.LIVE,
        issue_url=None,
        incomplete=[],
        error=None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        '{"detail": "room exists"}',
        '{"detail": "room exists", "room_id": 7}',
        '{"detail": ["not", "a", "message"], "room_id": "a:b"}',
    ],
    ids=["no-room-id", "room-id-not-a-string", "detail-not-a-string"],
)
def test_refusal_needs_a_message_and_a_room_id(payload):
    with pytest.raises(pydantic.ValidationError):
        api.Refusal.model_validate_json(payload)


def test_error_takes_any_detail():
    result = api.Error.model_validate_json('{"detail": [{"loc": ["body"]}]}')

    assert result.detail == [{"loc": ["body"]}]
