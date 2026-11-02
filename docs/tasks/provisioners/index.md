# Implementing a provisioner

A room provisioner is the service that creates a private RAG room for a user
the concierge's [`request_room`](../rooms/index.md#new-rooms-on-request) tool
asks on behalf of. What creating a room involves depends on the stack, so each
deployment runs a provisioner of its own. A `soliplex-template` stack's
`soliplex-provisioner` service is one. Every provisioner serves the same
endpoints: the [Provisioner REST API](../../reference/provisioner-rest-api.md).

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
  readiness watcher) must already be in place.
