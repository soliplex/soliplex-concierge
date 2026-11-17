# `soliplex-concierge-room`

**Function.** The in-room request formatter: it turns a user's new-room or
room-access request into a ready-to-file tracking-issue draft that the
about-room's `create_gitea_issue` tool files. See
[The concierge](../tasks/rooms/index.md) for how it fits the request flow.

**Where to find it.** Published as a release artifact of
[`soliplex/soliplex-concierge`](https://github.com/soliplex/soliplex-concierge/releases):
the [`room-skill-latest`](https://github.com/soliplex/soliplex-concierge/releases/tag/room-skill-latest)
pointer tracks the newest build (the asset is
`soliplex-concierge-room-skill.tar.gz`), and tagged software releases (`v*`)
attach the same tarball pinned to that version.

**Where to install it.** Into a **Soliplex installation**, as a filesystem
skill enabled in the about-room's configuration — *not* into a coding agent.
It runs as a sub-agent of the room agent.

**How to install it.** The [installer skill](soliplex-concierge-installer.md)
does this for you: it downloads the release into the stack's
`backend/environment/skills/` directory, where Soliplex discovers it, and
enables it on the about-room. It adds a `skill_configs` entry to
`installation.yaml` only when the stack keeps an explicit filesystem
allow-list. To wire it by hand, unpack the release tarball into the
installation's `skills/` directory (and add a `skill_configs` entry only if
you keep such an allow-list) — see the manual steps in
[Setup](../tasks/rooms/setup.md).

**Keeping it current.** To update the copy in a stack, re-run the
installer's `install_concierge.py` with `--force` against the stack, from a
coding agent (`--room-skill-version` pins a published tag). `--force` also
replaces the about-room itself, so save any edits to it first (see
[Setup](../tasks/rooms/setup.md#useful-flags)).

The installed copy has no update helper of its own. This skill runs inside the
room's agent, and room users must never reach machinery that rewrites files
and calls out to GitHub/PyPI, so the
[installer](soliplex-concierge-installer.md) **strips the published tarball's
`scripts/skill_versions.py`** from the copy it drops into a stack. If you
unpack the tarball into an installation by hand, delete that file yourself.
(In a copy kept outside any stack, the helper can `list`, `diff`, and
`upgrade` the skill against the published builds — see the
[`soliplex-skills` versions CLI docs](https://soliplex.github.io/soliplex-skills/mechanisms/versions-cli/).)
