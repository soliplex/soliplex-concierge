# The self-serve concierge

The concierge is a single Soliplex room — the **about-room** (created as
`about_<project>` by default) — that users chat with to request changes to the
installation. It answers two kinds of need:

- **Questions about Soliplex** — grounded in the `soliplex-docs` skill (and
  optionally this installation's own RAG database).
- **Room requests** — a **new room**, or **access** to an existing private
  room. A new room is created on the spot for a user the stack's room
  provisioner admits (see [New rooms on request](#new-rooms-on-request));
  every other request is filed as a tracking issue for an administrator to
  resolve.

Filing a request is a collaboration between these actors, all behind the single
chat the user sees:

| Actor | What it is | Role |
| --- | --- | --- |
| **Room agent** | the about-room's chat agent | talks to the user, gathers details, files the issue |
| **`create_gitea_issue` tool** | a Python tool bound to the room | renders the request as an issue and files it on the fixed tracking repository |
| **`request_room` / `check_room_request` tools** | Python tools bound to the room | ask the stack's room provisioner to create a room, and check on it |

## New rooms on request

A [`soliplex-template`](https://github.com/soliplex/soliplex-template) stack
runs a room provisioner service, `soliplex-provisioner`, which creates a
private room for a requester in one step and files its tracking issue itself.
For a new room, the room agent first asks for the room's name and purpose, then
calls `request_room`:

- **Accepted** (or **already requested**, when an earlier attempt of the
  same request reached the provisioner) — the room is being created, private
  to the user. The agent
  reports its id and tracking issue, and checks on it with
  `check_room_request` when asked: `pending`, `live` (refresh the room list),
  `incomplete` (an administrator must finish some steps) or `failed`.
- **Not admitted** — the provisioner admits only admins, and users granted
  WRITE on the reserved room id `__provision__`. For anyone else, the agent
  files the request as an issue, as below.
- **For someone else** — an admin may ask for a room owned by another person,
  or by everyone a predicate the installation registers recognizes (a role,
  say). The agent settles whom with them, as a claim and its exact value
  (`email` is `bharney@example.com`) or a predicate's name, plus the room id's
  owner part, and asks them to confirm before requesting it; it never writes
  an access rule itself. Anyone else who names an owner gets
  **owner not permitted**, and the agent offers the room as their own, or
  files the request as an issue naming whom it is for.
- **Failed** — a name already taken sends the agent back to the user for
  another; when the provisioner cannot take the request at all (for example,
  a stack without the service), the agent files it as an issue.
- **No answer** — the tool waits up to `request_timeout` seconds (120 by
  default), then asks once more. When the provisioner still does not
  answer, the room may yet be created, so the agent tells the user to look
  for it shortly, and files no issue.

The tools act as the user: each call carries a short-lived service token
holding the user's claims, signed with the installation's
`URL_SAFE_TOKEN_SECRET`.

## The request flow

Every request the provisioner does not take is filed as an issue:

1. **The user asks** for a new room or for access to a private one.

2. **The room agent gathers the details** with follow-up questions:
   - *New room* — proposed name and purpose, the documents or knowledge
     sources it should draw from, who should have access, and any tools or
     skills it should provide.
   - *Access request* — which room, and why access is needed.

3. **The room agent files the request** by calling `create_gitea_issue`
   **exactly once**, passing what it gathered as a typed `request`: a
   `NewRoomRequest` or a `RoomAccessRequest`. The target repository is fixed by
   the room's tool configuration, so the agent cannot file against an arbitrary
   repo. The tool:
   - renders the issue's title and Markdown body from the request, writing
     `*(not provided)*` for anything missing so the reviewer knows to follow up;
   - ensures the request-type label (`new-room` / `room-access`) exists,
     creating it if needed, and tags the issue with it;
   - **attaches the requesting user's profile** to the issue as a YAML file
     (`user-profile.yaml` by default), so the requester's identity is recorded
     without the agent transcribing it — see [Room access](../admin/room_access.md).
     If Gitea refuses the attachment (e.g. its `[attachment] ALLOWED_TYPES`
     lacks `.yaml`), the profile goes into a comment on the issue instead;
   - returns the issue number and URL.

4. **The room agent reports** the issue number and link back to the user. When
   filing fails, the tool's failure says whether the issue exists. If it does
   not, the agent shows the user the drafted title and body for manual
   submission. If it does or may, the agent sends the user to an administrator
   and does not file again. The agent never claims to have filed an issue it
   did not create.

## Things to know

- **Authentication is required.** The tool attributes every request to the
  signed-in user; an anonymous run is refused (`IssueNotFiled`, caused by
  `AnonymousUser`). Configure an
  [OIDC provider](https://soliplex.github.io/soliplex/config/oidc_providers/)
  so users are identified.
- **The Gitea account needs Write access.** Gitea lets a Read-only account open
  issues but silently drops their labels; the tool detects the missing label and
  reports it (`IssueFiledWithoutLabel`), so the agent sends the user to an
  administrator rather than leaving an untagged request to slip past triage
  (see issue #59). [Setup](setup.md) covers the token requirements.
- **The repository is fixed by the room config**, not chosen by the agent — the
  concierge can only file against the tracking repo you configured.

To stand up the about-room in an installation, see [Setup](setup.md).
