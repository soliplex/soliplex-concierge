"""Unit tests for ``soliplex_concierge.provisioner.access_rules``.

Predicates are registered with soliplex's own JSONPath environment for the
test's duration, as an installation's ``meta.jsonpath_functions`` would.
"""

import pytest
from soliplex import authz

from soliplex_concierge.provisioner import access_rules
from soliplex_concierge.provisioner import room_ids


def _is_foreman(claims):
    # Shaped like a deployment's predicate: the claims, or nothing.
    return claims if claims.get("job_title") == "foreman" else ()


@pytest.fixture
def is_foreman(monkeypatch):
    extensions = authz.the_jsonpath_environment.function_extensions
    monkeypatch.setitem(extensions, "is_foreman", _is_foreman)


def test_predicate_rule():
    result = access_rules.predicate_rule("is_foreman")

    assert result == "$[?is_foreman($)]"


@pytest.mark.parametrize(
    "claims, expected",
    [({"job_title": "foreman"}, True), ({"job_title": "quarry hand"}, False)],
    ids=["passes", "fails"],
)
def test_predicate_rule_applies_to_the_whole_claims(
    is_foreman, claims, expected
):
    rule = access_rules.predicate_rule("is_foreman")

    result = authz.the_jsonpath_environment.match(rule, claims)

    assert (result is not None) is expected


@pytest.mark.parametrize(
    "name", ["", "Is_foreman", "1st", "is-foreman", "is_foreman($)", "a b"]
)
def test_predicate_rule_rejects_a_bad_name(name):
    with pytest.raises(access_rules.BadPredicateName) as caught:
        access_rules.predicate_rule(name)

    assert isinstance(caught.value, room_ids.RoomIdError)
    assert caught.value.name == name


def test_registered_predicates(is_foreman):
    result = access_rules.registered_predicates()

    assert "is_foreman" in result
    assert not result & authz.BUILTIN_JSONPATH_FUNCTION_NAMES


@pytest.mark.parametrize(
    "known, listed",
    [({"is_staff", "is_cso"}, "is_cso, is_staff"), (set(), "none")],
    ids=["some", "none"],
)
def test_unknown_predicate(known, listed):
    result = access_rules.UnknownPredicate("is_foreman", known)

    assert isinstance(result, room_ids.RoomIdError)
    assert result.known == tuple(sorted(known))
    assert str(result) == (
        "'is_foreman' is not a predicate this installation registers "
        f"(it registers: {listed})"
    )
