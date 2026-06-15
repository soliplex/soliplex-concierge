---
name: soliplex-concierge-room
description: |
    Format a Soliplex room request into a ready-to-file tracking issue. Use
    for a NEW room request, or a request for ACCESS to an existing private
    room. Returns an issue title and Markdown body; the calling agent files
    it with the create_gitea_issue tool.
license: MIT
metadata:
  version: "0.4.0"
---

# Soliplex room-request concierge

You turn a room request into a tracking-issue **draft** -- a title and a
Markdown body -- for an administrator to review. You do **not** file the issue
yourself, and you cannot create rooms or grant access; the agent that called
you files the draft you return.

You run in isolation: everything you need is in the request passed to you. The
request describes one of two kinds of request -- pick the matching template and
fill it in:

1. **New room** -- the user wants a room that does not yet exist.
   Template: `assets/room_creation_request.md`.
2. **Access to an existing private room** -- the user wants access to a room
   that already exists.
   Template: `assets/room_access_request.md`.

## How to respond

1. Decide which of the two request types the request describes.
2. Read the matching template with the `read_resource` tool.
3. Fill in every placeholder (`<...>`) from the information in the request.
   For any field the request does not supply, write `*(not provided)*` rather
   than inventing a value, so the reviewer knows to follow up.
4. Choose a clear, specific one-line title, e.g. `New room request: <name>` or
   `Room access request: <room>`.
5. Return your result in **exactly** this shape and nothing else:

   ```text
   TYPE: <new-room | room-access>
   TITLE: <the one-line title>

   <the filled-in template body, in Markdown>
   ```

   The first line must begin with `TYPE:` and a space, followed by exactly
   `new-room` (for a new room) or `room-access` (for access to an existing
   private room). The second line must begin with `TITLE:` and a space,
   followed by the title; after one blank line comes the issue body. Do not
   wrap the result in code fences and do not add any commentary -- the calling
   agent parses this output directly to file the issue.

## Managing this skill's version

`scripts/skill_versions.py` lists, diffs, and upgrades published builds of this
skill against its GitHub releases. It is a small
[PEP 723](https://peps.python.org/pep-0723/) helper backed by the shared
[`soliplex-skills`](https://soliplex.github.io/soliplex-skills/) library, so run
it with `uv` (the first run fetches that library):

```bash
uv run scripts/skill_versions.py list              # published versions, newest first
uv run scripts/skill_versions.py diff [TAG]         # installed vs a published build (default: latest)
uv run scripts/skill_versions.py upgrade [TAG]      # install a published build in place (default: latest)
```

Two kinds of versions are published. **Release** builds are snapshots attached
to tagged software releases (`v…`) — stable milestones that only change when a
release is cut. **Rolling** builds (`room-skill-YYYY.MM.DD-<sha>`) are
continuous per-build snapshots, tagged with the build date and short commit
hash; the `room-skill-latest` pointer always tracks the newest one, so the
default `latest` target for `diff`/`upgrade` means "the current tip of the
rolling line." `list` shows both newest first (marking the installed copy and
the `latest` pointer); narrow it with `list --kind release` or
`list --kind rolling`. To stay on stable milestones rather than the rolling
tip, pass an explicit `v…` `TAG` to `diff`/`upgrade`.

Set `GITHUB_TOKEN`/`GH_TOKEN` to raise the GitHub API rate limit. The helper
needs network access to PyPI (to provision `soliplex-skills` on first run) and
to `api.github.com`/`github.com`.

> **Note — installed copies differ.** The above applies to a copy fetched into a
> coding agent. When the `soliplex-concierge-installer` skill drops this skill
> into a Soliplex stack, it runs inside a room agent rather than a coding agent,
> so the installer **strips this whole section and the
> `scripts/skill_versions.py` helper** from the installed copy — room users must
> never reach machinery that rewrites files and calls out to GitHub/PyPI. Update
> an installed copy by re-running the installer's `install_concierge.py` (with `--force`)
> against the stack from a coding agent.
