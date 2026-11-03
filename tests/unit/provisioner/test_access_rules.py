"""Unit tests for ``soliplex_concierge.provisioner.access_rules``.

The rules are checked against soliplex's own JSONPath environment, where a
predicate is registered for the test's duration, as an installation's
``meta.jsonpath_functions`` would.
"""

import pytest
from soliplex import authz

from soliplex_concierge.provisioner import access_rules


def _is_foreman(claims):
    # Shaped like a deployment's predicate: the claims, or nothing.
    return claims if claims.get("job_title") == "foreman" else ()


@pytest.fixture
def is_foreman(monkeypatch):
    extensions = authz.the_jsonpath_environment.function_extensions
    monkeypatch.setitem(extensions, "is_foreman", _is_foreman)


_BAD_CLAIMS = ["", "1st", "a.b", "a b", "a]"]


# ---- claim rules ----


@pytest.mark.parametrize(
    "claim, value, expected",
    [
        ("preferred_username", "alice", '$[?$.preferred_username == "alice"]'),
        ("email", "a@b.example", '$[?$.email == "a@b.example"]'),
        ("sub", 'say "hi"', '$[?$.sub == "say \\"hi\\""]'),
    ],
)
def test_claim_rule(claim, value, expected):
    result = access_rules.claim_rule(claim, value)

    assert result == expected
    assert result == authz.token_field_json_path(claim, value)


@pytest.mark.parametrize(
    "claims, expected",
    [({"email": "a@b.example"}, True), ({"email": "c@d.example"}, False)],
    ids=["matches", "differs"],
)
def test_claim_rule_applies_to_the_claim(claims, expected):
    rule = access_rules.claim_rule("email", "a@b.example")

    result = authz.the_jsonpath_environment.match(rule, claims)

    assert (result is not None) is expected


@pytest.mark.parametrize("claim", _BAD_CLAIMS)
def test_claim_rule_rejects_a_bad_claim(claim):
    with pytest.raises(access_rules.BadOwnerClaim) as caught:
        access_rules.claim_rule(claim, "alice")

    assert isinstance(caught.value, access_rules.AccessRuleError)
    assert caught.value.claim == claim
    assert "must be an identifier" in str(caught.value)


def test_check_claim():
    result = access_rules.check_claim("preferred_username")

    assert result is None


@pytest.mark.parametrize("claim", _BAD_CLAIMS)
def test_check_claim_rejects_a_bad_claim(claim):
    with pytest.raises(access_rules.BadOwnerClaim):
        access_rules.check_claim(claim)


def test_claim_value_rule():
    claims = {"email": "a@b.example", "sub": "a-sub"}

    result = access_rules.claim_value_rule(claims, "email")

    assert result == ("a@b.example", '$[?$.email == "a@b.example"]')


def test_claim_value_rule_missing_claim():
    claims = {"sub": "a-sub"}

    with pytest.raises(access_rules.ClaimMissing) as caught:
        access_rules.claim_value_rule(claims, "email")

    assert isinstance(caught.value, KeyError)
    assert caught.value.claim == "email"


@pytest.mark.parametrize("value", [12345, None, ["staff"]])
def test_claim_value_rule_not_a_string(value):
    claims = {"email": value}

    with pytest.raises(access_rules.ClaimNotAString) as caught:
        access_rules.claim_value_rule(claims, "email")

    assert isinstance(caught.value, ValueError)
    assert (caught.value.claim, caught.value.value) == ("email", value)
    assert "not a string" in str(caught.value)


def test_claim_value_rule_bad_claim():
    claims = {"a.b": "alice"}

    with pytest.raises(access_rules.BadOwnerClaim):
        access_rules.claim_value_rule(claims, "a.b")


# ---- predicate rules ----


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

    assert isinstance(caught.value, access_rules.AccessRuleError)
    assert caught.value.name == name


@pytest.mark.parametrize(
    "known, listed",
    [({"is_staff", "is_cso"}, "is_cso, is_staff"), (set(), "none")],
    ids=["some", "none"],
)
def test_unknown_predicate(known, listed):
    result = access_rules.UnknownPredicate("is_foreman", known)

    assert isinstance(result, access_rules.AccessRuleError)
    assert result.known == tuple(sorted(known))
    assert str(result) == (
        "'is_foreman' is not a predicate this installation registers "
        f"(it registers: {listed})"
    )
