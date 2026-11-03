import fake_service
import pytest

from soliplex_concierge.provisioner import contract


def _harness(*faults):
    return fake_service.FakeHarness(fake_service.create_app(frozenset(faults)))


def test_all_checks_lists_every_check():
    checks = {
        name
        for name in dir(contract)
        if name.startswith("check_") and name != "check_all"
    }

    result = {check.__name__ for check in contract.ALL_CHECKS}

    assert result == checks


@pytest.mark.anyio
async def test_check_all_passes_a_conforming_service():
    harness = _harness()

    result = await contract.check_all(harness)

    assert result is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "check, fault, message",
    [
        (
            contract.check_health,
            "no-version-header",
            "expected the header 'X-Soliplex-Provisioner-API: 1', got None",
        ),
        (
            contract.check_health,
            "unhealthy",
            "GET /ok: expected HTTP 200, got HTTP 503",
        ),
        (
            contract.check_requires_a_token,
            "no-auth",
            "POST /rooms with no token: expected HTTP 401, got HTTP 202",
        ),
        (
            contract.check_requires_a_token,
            "no-www-authenticate",
            "expected a 'WWW-Authenticate: Bearer' header",
        ),
        (
            contract.check_refuses_an_invalid_token,
            "trust-any-token",
            "with an invalid token: expected HTTP 401, got HTTP 202",
        ),
        (
            contract.check_refuses_the_unadmitted,
            "admit-everyone",
            "by an unadmitted requester: expected HTTP 403, got HTTP 202",
        ),
        (
            contract.check_rejects_a_malformed_body,
            "lenient-body",
            "with an unknown field: expected HTTP 422, got HTTP 202",
        ),
        (
            contract.check_rejects_a_bad_slug,
            "accept-any-slug",
            "with the slug '../escape': expected HTTP 400, got HTTP 202",
        ),
        (
            contract.check_refuses_a_named_owner_from_a_non_admin,
            "anyone-names-owners",
            "by a non-admin: expected HTTP 403, got HTTP 202",
        ),
        (
            contract.check_rejects_a_bad_owner,
            "accept-any-owner",
            "naming the owner '../escape', by an admin: expected HTTP 400",
        ),
        (
            contract.check_admin_names_an_owner,
            "ignore-named-owner",
            "expected a room id owned by",
        ),
        (
            contract.check_admin_names_an_owner,
            "malformed-accepted",
            "naming an owner, by an admin: the answer does not parse",
        ),
        (
            contract.check_accepts_and_reports,
            "accept-with-200",
            "by an admitted requester: expected HTTP 202, got HTTP 200",
        ),
        (
            contract.check_accepts_and_reports,
            "malformed-accepted",
            "POST /rooms: the answer does not parse as Accepted",
        ),
        (
            contract.check_accepts_and_reports,
            "wrong-room-id",
            "got 'someone:else'",
        ),
        (
            contract.check_hides_requests_from_others,
            "show-others",
            "by another requester: expected HTTP 404, got HTTP 200",
        ),
        (
            contract.check_hides_requests_from_others,
            "hide-from-admins",
            "by an admin: expected HTTP 200, got HTTP 404",
        ),
        (
            contract.check_reports_unknown_requests,
            "unknown-as-pending",
            "never requested: expected HTTP 404, got HTTP 200",
        ),
        (
            contract.check_refuses_an_existing_room,
            "overwrite",
            "for an existing room: expected HTTP 409, got HTTP 202",
        ),
    ],
)
async def test_check_catches_a_violation(check, fault, message):
    harness = _harness(fault)

    with pytest.raises(contract.ContractViolation) as caught:
        await check(harness)

    assert message in str(caught.value)
