"""Fake adapters for testing the provisioner's core.

They share one :class:`Script`, which records each call in ``journal``
(as ``"<adapter>.<method>"``) and says what each call does: raise
(``failures``), report clashes (``clashes``, by adapter), or leave steps
unfinished (``unfinished``, by method).
"""

import contextlib
import dataclasses

from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import provision

COMMIT = "c0ffee"


@dataclasses.dataclass(frozen=True)
class FakeIssue:
    url: str = "https://gitea.example.com/ops/requests/issues/7"
    number: int = 7


@dataclasses.dataclass
class Script:
    journal: list[str] = dataclasses.field(default_factory=list)
    failures: dict[str, Exception] = dataclasses.field(default_factory=dict)
    undo_failures: dict[str, Exception] = dataclasses.field(
        default_factory=dict
    )
    clashes: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    unfinished: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    has_room: bool = False
    uploads_exist: bool = False
    reported: list[Exception] = dataclasses.field(default_factory=list)
    resolved: list[interfaces.Provisioned] = dataclasses.field(
        default_factory=list
    )

    def step(self, name: str) -> None:
        self.journal.append(name)
        if name in self.failures:
            raise self.failures[name]

    def undo(self, name: str) -> interfaces.Undo:
        def run():
            self.journal.append(f"undo {name}")
            if name in self.undo_failures:
                raise self.undo_failures[name]

        return interfaces.Undo(description=f"undo {name}", run=run)

    def clashes_of(self, adapter: str) -> list[str]:
        self.step(f"{adapter}.clashes")
        return list(self.clashes.get(adapter, ()))

    def unfinished_of(self, name: str) -> list[str]:
        self.step(name)
        return list(self.unfinished.get(name, ()))


@dataclasses.dataclass
class FakeStore:
    script: Script

    @contextlib.contextmanager
    def locked(self):
        self.script.step("store.lock")
        try:
            yield
        finally:
            self.script.journal.append("store.unlock")

    def prepare(self):
        self.script.step("store.prepare")

    def has_room(self, request):
        self.script.step("store.has_room")
        return self.script.has_room

    def clashes(self, request):
        return self.script.clashes_of("store")

    def add_room(self, request, issue):
        self.script.step("store.add_room")
        return self.script.undo("store.add_room")

    def commit(self, request, issue):
        self.script.step("store.commit")
        return COMMIT

    def publish(self):
        return self.script.unfinished_of("store.publish")


@dataclasses.dataclass
class FakeRegistrar:
    script: Script

    def clashes(self, request):
        return self.script.clashes_of("registrar")

    def register(self, request):
        self.script.step("registrar.register")
        return self.script.undo("registrar.register")


@dataclasses.dataclass
class FakeUploads:
    script: Script

    def clashes(self, request):
        return self.script.clashes_of("uploads")

    def create(self, request):
        self.script.step("uploads.create")
        if self.script.uploads_exist:
            return None
        return self.script.undo("uploads.create")


@dataclasses.dataclass
class FakeAuthz:
    script: Script

    def clashes(self, request):
        return self.script.clashes_of("authz")

    def grant_owner(self, request):
        self.script.step("authz.grant_owner")
        return self.script.undo("authz.grant_owner")

    def audit(self):
        self.script.step("authz.audit")


@dataclasses.dataclass
class FakeBackend:
    script: Script

    def check_access(self):
        self.script.step("backend.check_access")

    def reload_rooms(self, request):
        return self.script.unfinished_of("backend.reload_rooms")


@dataclasses.dataclass
class FakeTracker:
    script: Script

    def file(self, request):
        self.script.step("tracker.file")
        return FakeIssue()

    def report_failure(self, issue, error):
        self.script.reported.append(error)
        self.script.step("tracker.report_failure")

    def resolve(self, issue, provisioned):
        self.script.resolved.append(provisioned)
        return self.script.unfinished_of("tracker.resolve")


def make_provisioner(
    script: Script, *, tracker: bool = True, **kwargs
) -> provision.Provisioner:
    return provision.Provisioner(
        store=FakeStore(script),
        registrar=FakeRegistrar(script),
        uploads=FakeUploads(script),
        authz=FakeAuthz(script),
        backend=FakeBackend(script),
        tracker=FakeTracker(script) if tracker else None,
        **kwargs,
    )
