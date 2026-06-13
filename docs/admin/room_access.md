# Resolving a room-access request

A room-access request (the `room-access` label) asks for an existing private
room to be opened to a specific user. Resolving it means understanding *who*
wants access to *which* room, then granting it with the
`soliplex-cli room-authz` command set. The `soliplex-concierge-admin` skill
helps you read the request and records the decision with the `approved` /
`denied` label.

## 1. Understand the request

Find the open access requests and read one:

```sh
uv run scripts/gitea_issues.py search --type room-access \
    --owner <owner> --repo <repo>
uv run scripts/gitea_issues.py show <number> --owner <owner> --repo <repo>
```

The body (the room-skill's room-access template) gives the **room** and the
**justification**. The requester's identity, though, comes from the **attached
user profile**, not the body: when the concierge filed the issue,
`create_gitea_issue` attached the signed-in user's profile as a YAML file
(`user-profile.yaml` by default). It holds the OIDC claims Soliplex knows about
the user — typically their `email` and `preferred_username` — which are exactly
the discriminators you grant against below.

Open the attachment from the issue to read the claims. (An installation may set
`exclude_claims` on the tool to keep selected fields — e.g. `email` — out of the
tracking repo; if the discriminator you need was excluded, fall back to the
other, or ask the requester.)

## 2. Grant access

Granting access mutates the running stack's authorization database, so run the
`soliplex-cli room-authz` commands against the stack through a one-off `backend`
container, from the stack directory:

```sh
# Make the room private if it isn't already — add-acl-entry needs a RoomPolicy.
docker compose run --rm backend \
  /app/.venv/bin/soliplex-cli room-authz make-private /environment <room_id>

# Allow the requesting user, matched by an OIDC claim from their profile.
docker compose run --rm backend \
  /app/.venv/bin/soliplex-cli room-authz add-acl-entry \
    --allow --email <user-email> /environment <room_id>
```

Notes:

- `/environment` is the in-container installation path.
- Match the user with the discriminator that fits their profile: `--email`
  `<email>` or `--preferred-username <name>`. (`add-acl-entry` also supports
  `--everyone` and `--authenticated` catch-alls, and `--deny` entries.)
- Verify the result with
  `soliplex-cli room-authz show /environment <room_id>`.

These access mechanics are documented in `soliplex-docs`; see the
[room configuration](https://soliplex.github.io/soliplex/config/rooms/) and
[OIDC authentication](https://soliplex.github.io/soliplex/config/oidc_providers/)
docs.

## 3. Record the decision

The `approve` / `deny` subcommands close the issue and apply the matching
decision label in one step.

**Approve** — once the grant has actually succeeded:

```sh
uv run scripts/gitea_issues.py approve <number> --owner <owner> \
    --repo <repo> \
    --body "Granted <user> access to '<room>' via room-authz add-acl-entry."
```

**Deny** — no stack change is needed; just record the decision and the reason:

```sh
uv run scripts/gitea_issues.py deny <number> --owner <owner> \
    --repo <repo> --body "<reason for denial>"
```

Never close a request you could not fulfil — if the grant fails or the request
is unclear, comment what is blocking it and leave the issue open.
