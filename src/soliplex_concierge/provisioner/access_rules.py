"""Access rules for a room's named owner, in the forms a room agent may use.

An access rule is a soliplex ACL JSONPath over a user's token claims. A
room agent never writes one: it names an owner in one of two forms, and
the rule is built from it here.

- a claim and a static value (``email`` is ``bharney@example.com``):
  :func:`soliplex_concierge.provisioner.room_ids.claim_json_path`
- a *predicate*: a function the installation registers under
  ``meta.jsonpath_functions``, applied to the claims as a whole
  (:func:`predicate_rule`)

:func:`registered_predicates` names the predicates soliplex's JSONPath
environment holds: in a process that has loaded the installation (the
backend, say), those the installation registers.
"""

import re

from soliplex import authz

from soliplex_concierge.provisioner import room_ids

# A JSONPath function extension's name.
PREDICATE_RE = re.compile(r"[a-z][a-z0-9_]*")


class BadPredicateName(room_ids.RoomIdError):
    def __init__(self, name):
        self.name = name
        super().__init__(
            f"predicate name {name!r} must match {PREDICATE_RE.pattern}"
        )


class UnknownPredicate(room_ids.RoomIdError):
    def __init__(self, name, known):
        self.name, self.known = name, tuple(sorted(known))
        listed = ", ".join(self.known) or "none"
        super().__init__(
            f"{name!r} is not a predicate this installation registers "
            f"(it registers: {listed})"
        )


def predicate_rule(name: str) -> str:
    """The rule matching users whose claims the predicate ``name`` passes.

    Raises :class:`BadPredicateName` when ``name`` cannot name one.
    """
    if not PREDICATE_RE.fullmatch(name):
        raise BadPredicateName(name)
    return f"$[?{name}($)]"


def registered_predicates() -> frozenset[str]:
    """The predicates registered with soliplex's JSONPath environment.

    That is, its function extensions other than the RFC 9535 built-ins.
    """
    extensions = authz.the_jsonpath_environment.function_extensions
    return frozenset(extensions) - authz.BUILTIN_JSONPATH_FUNCTION_NAMES
