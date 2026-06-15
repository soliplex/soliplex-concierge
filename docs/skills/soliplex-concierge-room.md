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
does this for you: it downloads the release into the stack's `skills/`
directory and enables it in `installation.yaml`. To wire it by hand, download
the release tarball into the installation's `skills/` directory and add it to
`filesystem_skills_paths` / `skill_configs` — see the manual steps in
[Setup](../tasks/rooms/setup.md).

**Keeping it current.** After installing the published tarball in a coding
agent, the skill contains `scripts/skill_versions.py`, which can `list`,
`diff`, and `upgrade` the skill against the published builds — see the
[`soliplex-skills` versions CLI docs](https://soliplex.github.io/soliplex-skills/mechanisms/versions-cli/).

When installed in a Soliplex room, however, this skill runs inside the room's
agent rather than a coding agent. Room users should never reach machinery that
rewrites files and calls out to GitHub/PyPI, and therefore the
[installer](soliplex-concierge-installer.md) **strips that helper
from the copy it drops into a stack**. To update an installed copy, re-run the
installer's `install_concierge.py` (with `--force`) against the stack
from a coding agent.
