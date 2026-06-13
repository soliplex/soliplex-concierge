# `soliplex-concierge-installer`

**Function.** Wires the concierge into a target Soliplex stack — creating the
about-room, installing the room and docs skills, merging the `installation.yaml`
entries, and dropping in the admin CLI. It is the convenient path described in
[Setup](../tasks/rooms/setup.md).

**Where to find it.** Published as a release artifact of
[`soliplex/soliplex-concierge`](https://github.com/soliplex/soliplex-concierge/releases):
the [`installer-skill-latest`](https://github.com/soliplex/soliplex-concierge/releases/tag/installer-skill-latest)
pointer always tracks the newest build (the asset is
`soliplex-concierge-installer-skill.tar.gz`), and every tagged software release
(`v*`) attaches the same tarball pinned to that version.

**Where to install it.** Into an **external coding agent** (e.g. Claude Code) —
the skill runs there to operate on your stack, not inside the Soliplex server.

**How to install it.** Download and extract the release tarball into your coding
agent's skills directory (where it discovers
[Agent Skills](https://agentskills.io)); the
[`soliplex-skills` installation guide](https://soliplex.github.io/soliplex-skills/mechanisms/installation/)
covers fetching a published skill. Once the agent has the skill, run its bundled
`scripts/apply.py` as shown in [Setup](../tasks/rooms/setup.md).

**Keeping it current.** Each published copy bundles `scripts/skill_versions.py`,
which can `list`, `diff`, and `upgrade` the installed skill against the published
builds — see the
[`soliplex-skills` versions CLI docs](https://soliplex.github.io/soliplex-skills/mechanisms/versions-cli/).
