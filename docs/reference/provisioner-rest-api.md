# Provisioner REST API

A room provisioner creates a private RAG room on a requesting user's behalf.
The concierge's [`request_room` and `check_room_request`
tools](api.md#soliplex_conciergetoolsprovisioner) are its client. Each
deployment serves the API with an implementation of its own, because what
creating a room involves depends on the stack: a `soliplex-template` stack's
`soliplex-provisioner` service is one such implementation.

This page specifies the HTTP endpoints every implementation serves. The
models and constants it names live in `soliplex_concierge.provisioner.api`
(see the [Python API](api.md#soliplex_conciergeprovisionerapi)). To check an
implementation against this contract, see
[Implementing a provisioner](../tasks/provisioners/index.md).

## Credentials

Every request to `/rooms` carries a Bearer soliplex *service token*. The token
holds the requesting user's claims and is signed with the installation's
`URL_SAFE_TOKEN_SECRET` (`api.SERVICE_TOKEN_SECRET`): the caller mints it with
`soliplex.token_mint`, and the service validates it with the same secret. A
missing or invalid token gets `401`, with a `WWW-Authenticate: Bearer` header.

## Admission

The service admits a requester who is either of these:

- an admin of the installation
- a holder of WRITE on the reserved room id `__provision__`
  (`api.ADMISSION_ROOM_ID`)

It answers any other requester `403`.

## Routes

### `POST /rooms`

The body is a `RoomRequest`:

```json
{"name": "Q3 Plans", "description": "Plans for Q3", "slug": "q3-plans"}
```

`description` and `slug` are optional, and no other field is allowed. The new
room's id is `<owner><separator><slug>`, the separator being the
deployment's (by default `--`, as in `alice--q3-plans`). The owner part
derives from one of the requester's claims: lowercased by default, with any
separator in it replaced. The slug derives from the request's `name`, unless
the request asks for a specific `slug`; one that does not fit (including one
containing the separator) is a `400`, and the caller may retry with another.

An admin may make someone other than themselves the room's owner with the
optional `owner`, a `RoomOwner`. `owner.owner` is the room id's owner part,
normalized as a claimed one is (lowercased by default, any separator
replaced).
Whom the room is granted to is given in one of two forms:

- a claim and its exact value, for one person:

    ```json
    {"name": "Q3 Plans",
     "owner": {"owner": "bharney", "claim": "email",
               "value": "bharney@example.com"}}
    ```

- a predicate the installation registers under `meta.jsonpath_functions`,
  applied to a user's claims as a whole, for everyone it recognizes:

    ```json
    {"name": "Quarry Notes",
     "owner": {"owner": "foremen", "predicate": "is_foreman"}}
    ```

The service builds the access rule (a soliplex ACL JSONPath) from the form;
no caller writes one, and an `owner` with a `json_path` is a `422`. The
requester is still the one who asked, and may see the request.

| Status | Body | Meaning |
|---|---|---|
| `202` | `Accepted`: `room_id`, `issue_url` | Admitted; the room is created in the background |
| `400` | `Error`: `detail` | The name, slug or named owner yields no usable room id, or the owner's predicate is not registered |
| `401` | `Error` | No service token, or one that is not valid |
| `403` | `Error` | The requester is not admitted, or names an owner without being an admin |
| `409` | `Refusal`: `detail`, `room_id` | The room exists, or a request for it is in flight |
| `422` | | The body is not a `RoomRequest` (including an `owner` in neither form, or both) |
| `500` | `Error` | The service failed |

A `409` names the room it refuses, because the caller cannot work the room id
out for itself. The concierge relies on this when it retries a request that
timed out: a `409` naming the room means the first attempt got through.

### `GET /rooms/{room_id}`

The room id is quoted as one path segment (`api.room_path`); the default
`--` separator needs no quoting, and a legacy `:` is left as is.

| Status | Body | Meaning |
|---|---|---|
| `200` | `RequestState` | The request's state |
| `401` | `Error` | As for `POST /rooms` |
| `404` | `Error` | No request for that room, or one the caller may not see |

Only a request's requester and admins may see it. Anyone else gets `404`, as
if there were no such request.

A `RequestState` carries:

- `room_id`
- `state`: one of the states below
- `issue_url`: the request's tracking issue, if any
- `incomplete`: the steps an admin must finish
- `error`: why the room was not created

The states:

- `pending`: the room is being created
- `live`: the room is ready
- `incomplete`: the room exists, but an admin must finish the steps in
  `incomplete`
- `failed`: the room was not created; `error` says why

### `GET /ok`

For health checks; it needs no token.

| Status | Body | Meaning |
|---|---|---|
| `200` | `OK` (plain text) | The service can create rooms |
| `503` | plain text, saying why | It cannot yet: for instance, the backend does not admit the provisioner itself |

## Versions

Every response carries the header `X-Soliplex-Provisioner-API`
(`api.API_VERSION_HEADER`), with the version of the API the service implements
(`api.API_VERSION`, currently `1`).

The concierge's tools fail with `IncompatibleService` when a service names
another version. They take an answer without the header to be version 1's,
because the services that predate the header implement it.
