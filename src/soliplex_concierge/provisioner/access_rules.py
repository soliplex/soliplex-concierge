"""Access rules for a room's owner, in the forms a room agent may use.

An access rule is a soliplex ACL JSONPath over a user's token claims. A
room agent never writes one: an owner is named in one of two forms, and
the rule is built from it here.

- a claim and a static value (``email`` is ``bharney@example.com``):
  :func:`claim_rule`
- a *predicate*, applied to the claims as a whole: :func:`predicate_rule`

A predicate is a function the installation registers under
``meta.jsonpath_functions``. To list the ones a process holds, see
:func:`soliplex_concierge.provisioner.soliplex_api.registered_predicates`.

Stdlib only.
"""

import collections.abc
import json
import re

# A claim usable in an ACL entry's JSONPath query ('$.<claim>').
CLAIM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A JSONPath function extension's name.
PREDICATE_RE = re.compile(r"[a-z][a-z0-9_]*")


class AccessRuleError(Exception):
    """An owner's access rule cannot be built."""


class BadOwnerClaim(AccessRuleError):
    def __init__(self, claim):
        self.claim = claim
        super().__init__(
            f"owner claim {claim!r} must be an identifier ({CLAIM_RE.pattern})"
        )


class BadPredicateName(AccessRuleError):
    def __init__(self, name):
        self.name = name
        super().__init__(
            f"predicate name {name!r} must match {PREDICATE_RE.pattern}"
        )


class UnknownPredicate(AccessRuleError):
    def __init__(self, name, known):
        self.name, self.known = name, tuple(sorted(known))
        listed = ", ".join(self.known) or "none"
        super().__init__(
            f"{name!r} is not a predicate this installation registers "
            f"(it registers: {listed})"
        )


class ClaimMissing(KeyError):
    """The claims lack the claim a rule would match."""

    def __init__(self, claim):
        self.claim = claim
        super().__init__(claim)


class ClaimNotAString(ValueError):
    """The claim a rule would match does not hold a string."""

    def __init__(self, claim, value):
        self.claim, self.value = claim, value
        super().__init__(f"claim {claim!r} holds {value!r}, not a string")


def check_claim(claim: str) -> None:
    """Raise :class:`BadOwnerClaim` unless a rule can query ``claim``."""
    if not CLAIM_RE.fullmatch(claim):
        raise BadOwnerClaim(claim)


def claim_rule(claim: str, value: str) -> str:
    """The rule matching users whose ``claim`` is ``value``.

    Raises :class:`BadOwnerClaim` when ``claim`` is not an identifier.
    """
    check_claim(claim)
    return f"$[?$.{claim} == {json.dumps(value)}]"


def claim_value_rule(
    claims: collections.abc.Mapping, claim: str
) -> tuple[str, str]:
    """The value of ``claim`` in ``claims``, and the rule matching it.

    Raises :class:`BadOwnerClaim` when ``claim`` is not an identifier,
    :class:`ClaimMissing` (a :class:`KeyError`) when ``claims`` lacks it,
    and :class:`ClaimNotAString` (a :class:`ValueError`) when its value is
    not a string.
    """
    check_claim(claim)
    try:
        value = claims[claim]
    except KeyError as error:
        raise ClaimMissing(claim) from error
    if not isinstance(value, str):
        raise ClaimNotAString(claim, value)
    return value, claim_rule(claim, value)


def predicate_rule(name: str) -> str:
    """The rule matching users whose claims the predicate ``name`` passes.

    Raises :class:`BadPredicateName` when ``name`` cannot name one.
    """
    if not PREDICATE_RE.fullmatch(name):
        raise BadPredicateName(name)
    return f"$[?{name}($)]"
