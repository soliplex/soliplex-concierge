# Implementing a provisioner

A room provisioner is the service that creates a private RAG room for a user
the concierge's [`request_room`](../rooms/index.md#new-rooms-on-request) tool
asks on behalf of. What creating a room involves depends on the stack, so each
deployment runs a provisioner of its own. A `soliplex-template` stack's
`soliplex-provisioner` service is one. Every provisioner serves the same
endpoints: the [Provisioner REST API](../../reference/provisioner-rest-api.md).

## Building on the concierge's core

`soliplex_concierge.provisioner` gives a provisioner the steps of creating a
room, and their guarantees; the deployment supplies the work each step does.
The split is strict: the core passes data (the request, the tracking issue,
each step's outcome) and writes no files and no prose of its own. Every room
config, `REQUEST.md`, issue body, ingester registration and piece of advice
to an operator comes from the deployment's adapters, in its own words.

### What the core does

`provision.provision_room` runs three phases; each also runs on its own
(`plan_room`, `admit_room`, `create_room`), which is how a service answers a
request before the room exists.

- **Planning** works out the room's owner and id, changing nothing.
- **Admission** checks that the backend admits the provisioner, then, holding
  the store, refuses a room whose id anything uses already and files the
  tracking issue.
- **Creation**, holding the store again, checks again, then writes the room's
  files, creates its uploads area, registers its database, makes it private to
  its owner, audits the installation's rooms, commits and publishes. Without
  the store, it then has the backend load the room and resolves the issue.

The guarantees:

- every refusal comes before anything is created
- a failure before the commit undoes what this run created, and only that,
  newest first, then reports on the tracking issue, which stays open
- after the commit the room stands: a step that fails is reported, with how to
  finish it, rather than undone

### What your adapters do

A `provision.Provisioner` gathers one adapter for each part of the stack. The
interfaces are in `soliplex_concierge.provisioner.interfaces`:

- `RoomStore`: where the room's own files live, and their history
- `DatabaseRegistrar`: the room's database, and how it is filled
- `UploadsArea`: where the room's documents are uploaded
- `Authz`: who may use the room
- `Backend`: the Soliplex backend that serves the room
- `IssueTracker` (optional): the request's tracking issue

They share three conventions:

- `clashes(request)` describes, in the adapter's words, each thing named for
  the room that exists already. It changes nothing, and may raise to refuse
  the request outright.
- A step that creates something returns the `Undo` that removes it again, or
  `None` when it created nothing.
- A step after the commit (`publish`, `reload_rooms`, `resolve`) returns what
  remains to do, each a sentence saying how to finish it.

### The owner and the requester

A `RoomRequest` keeps who asked (`requester_claims`) apart from whom the room
belongs to (`owner`, a `room_ids.RoomOwner`). The owner pairs the room id's
owner part with its access rule, a soliplex ACL JSONPath over a user's claims:

- by default, `plan_room` derives the requester's own owner from one of their
  claims (`Provisioner.owner_claim`, flattened when `flatten_owner` is set,
  lowercased unless `lowercase_owner` is false), with a rule matching that
  claim's raw value
- given `owner=room_ids.named_owner(...)`, it makes someone else the owner,
  for instance everyone a predicate recognizes; whether the requester may do
  that is for the caller to decide

Either way, any `Provisioner.room_id_separator` in the owner part is
replaced, so a room id splits into owner and slug one way only; a requested
slug containing the separator is refused instead.

Build a named owner's rule with `access_rules`, never by hand:
`claim_rule(claim, value)` for one person, or `predicate_rule(name)` for a
predicate the installation registers under `meta.jsonpath_functions`. A
service built with `service.create_app` does this itself, from the request's
`owner`, and refuses a predicate your `Authz.predicates()` does not list.

For example, with a `provisioner` built as in [The toolkit](#the-toolkit)
and its defaults, a user asking for a room for themselves gives it a title;
the slug derives from that, and the owner part from their
`preferred_username`, lowercased:

```python
request = provision.plan_room(
    {"preferred_username": "Alice.Smith"},
    name="Q3 Plans & Budget",
    provisioner=provisioner,
)
request.room          # 'alice.smith--q3-plans-budget'
request.owner         # RoomOwner(owner='alice.smith',
                      #   json_path='$[?$.preferred_username == "Alice.Smith"]')
```

The access rule matches the raw claim value, `Alice.Smith`, so the
lowercasing never costs Alice her room.

An admin creating a room for a group names its owner, identified by a
predicate the installation registers (here `is_foreman`); the owner part is
normalized the same way, and the admin stays the requester:

```python
foremen = room_ids.named_owner(
    "Foremen", access_rules.predicate_rule("is_foreman")
)
request = provision.plan_room(
    admin_claims,
    name="Quarry Notes",
    provisioner=provisioner,
    owner=foremen,
)
request.room          # 'foremen--quarry-notes'
request.owner         # RoomOwner(owner='foremen',
                      #   json_path='$[?is_foreman($)]')
request.requester_claims is admin_claims   # True
```

`Authz.grant_owner` grants the room to `request.owner.json_path`, whoever
that is.

### The toolkit

Adapters can build on these modules, which do the mechanical work and leave
the wording to you:

- `landing_clone`: a git clone of rooms, with its writer's lock, committing a
  room with its requester as author, and pushing
- `ingester`: reading a `haiku.rag serve` ingester's config and its
  `databases_include` fragments, and asking it to reload
- `soliplex_cli`: running `soliplex-cli` (or a CLI offering the same
  commands) for room policies and audits
- `soliplex_api`: reading the signing secret, minting service tokens, and
  calling the backend's installation API
- the concierge's own Gitea calls, `tools.gitea.create_gitea_issue_sync` and
  `gitea_admin`, for an issue tracker on Gitea

For example, an uploads area on a local directory:

```python
import dataclasses
import pathlib
import shutil

from soliplex_concierge.provisioner import interfaces


@dataclasses.dataclass(frozen=True)
class LocalUploads:
    root: pathlib.Path

    def clashes(self, request):
        directory = self.root / request.room
        if directory.is_dir() and any(directory.iterdir()):
            return [f"documents already uploaded to {directory}"]
        return []

    def create(self, request):
        directory = self.root / request.room
        if directory.exists():
            return None
        directory.mkdir(parents=True)
        return interfaces.Undo(
            description=f"remove the uploads directory {directory}",
            run=lambda: shutil.rmtree(directory, ignore_errors=True),
        )
```

With an adapter for each part, provisioning a room is one call:

```python
from soliplex_concierge.provisioner import provision

provisioner = provision.Provisioner(
    store=store,
    registrar=registrar,
    uploads=LocalUploads(pathlib.Path("/uploads/rooms")),
    authz=authz,
    backend=backend,
    tracker=tracker,  # or None
)
provisioned = provision.provision_room(
    requester_claims, name="Q3 Plans", provisioner=provisioner
)
```

`provisioned.unfinished` lists what remains to do once the room is committed;
it is empty when the room is fully live.

## Serving it

`soliplex_concierge.provisioner.service.create_app(provisioner, admission)`
builds a FastAPI application serving the
[Provisioner REST API](../../reference/provisioner-rest-api.md) over your
adapters. Besides the `Provisioner`, it takes an `interfaces.Admission`, which
decides who is asking and whether they may:

- `claims(token)`: the claims of the caller's service token, raising
  `interfaces.InvalidToken` (or a subclass) when it is not valid
- `is_admitted(token)`: whether its holder may request rooms (an admin, or
  WRITE on `__provision__`)
- `is_admin(token)`: whether its holder is an admin, who may name a room's
  owner and see anyone's requests

The toolkit has the pieces for a soliplex installation:
`soliplex_api.validate_service_token` (its `InvalidServiceToken` is an
`InvalidToken`), and `soliplex_cli.is_admin` and `soliplex_cli.may`, the
`admin-users check` and `room-authz check` verdicts.

The application's lifespan starts a watcher that calls your
`Backend.check_access` until the backend admits the provisioner; until then,
`GET /ok` answers `503` with the reason. Raise `interfaces.AccessRefused` (or
a subclass, such as `soliplex_api.AccessDenied`) when the backend refuses the
provisioner: the watcher logs it as an error and checks again every
`service.ACCESS_RETRY` seconds. Any other error counts as the backend being
unreachable: a warning, checked again every `service.BACKEND_RETRY` seconds.

Your wiring module builds the adapters, and a factory returns the application:

```python
from soliplex_concierge.provisioner import provision
from soliplex_concierge.provisioner import service


def create_app():
    provisioner = provision.Provisioner(...)  # your adapters
    return service.create_app(provisioner, MyAdmission(...))
```

```bash
uvicorn --factory my_deployment.provisioner:create_app --port 8090
```

Run one worker: the service keeps its requests' state in memory. A restart
loses it, and the tracking issue remains the record.

## Checking an implementation against the contract

`soliplex_concierge.provisioner.contract` checks that an implementation keeps
the contract. It drives the implementation's ASGI application in-process and
raises `ContractViolation`, saying what the application answered, when the
application breaks the contract.

### Supply a harness

A `Harness` is any object with these attributes:

- `app`: the ASGI application, ready to admit requests (its `GET /ok` answers
  `200`)
- `admitted_token`: a valid service token for a requester the application
  admits, who is not an admin
- `other_token`: a valid service token for another such requester, whose rooms
  get another owner part
- `admin_token`: a valid service token for an admin
- `not_admitted_token`: a valid service token for a requester the application
  does not admit
- `occupy(name)`: make the room that `admitted_token`'s requester would get for
  a request named `name` exist already
- optionally, `room_id_separator`: the separator in the room ids the
  application composes (by default `room_ids.DEFAULT_SEPARATOR`, `--`)

### Run the checks

Await the checks from your implementation's own tests:

```python
import pytest

from soliplex_concierge.provisioner import contract


@pytest.mark.anyio
async def test_keeps_the_provisioner_contract(harness):
    await contract.check_all(harness)
```

`check_all` runs every check in `contract.ALL_CHECKS` and stops at the first
failure. Each `check_*` function also runs on its own, which helps when you
are working on one part of the contract.

Keep these in mind when you write the harness:

- The checks request rooms with fresh, unique names, so they do not depend on
  one another. A check that requests a room may leave it created, so point the
  harness at a disposable stack or fakes.
- The checks drive the application through `httpx2.ASGITransport`, which does
  not run its lifespan: anything the lifespan would start (such as a
  readiness watcher) must already be in place. For an application from
  `service.create_app`, call `app.state.access.update(True)` in the harness.
