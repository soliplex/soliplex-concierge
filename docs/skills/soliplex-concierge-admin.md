# `soliplex-concierge-admin`

**Function.** Resolves the request issues the concierge files: list and filter
them, follow up, carry out the operation, and close with a decision. Its
`gitea_issues.py` CLI and workflow are documented under
[Administering requests](../tasks/admin/index.md).

**Where to find it.** Published as a release artifact of
[`soliplex/soliplex-concierge`](https://github.com/soliplex/soliplex-concierge/releases):
the [`admin-skill-latest`](https://github.com/soliplex/soliplex-concierge/releases/tag/admin-skill-latest)
pointer tracks the newest build (the asset is
`soliplex-concierge-admin-skill.tar.gz`), and tagged software releases (`v*`)
attach the same tarball pinned to that version.

**Where to install it.** Into an **external coding agent** (e.g. Claude Code) —
the admin works against the Gitea repository and the deployed stack from there,
not inside the Soliplex server. (The [installer](soliplex-concierge-installer.md)
separately drops a copy of the `gitea_issues.py` CLI into the stack at
`scripts/gitea_issues.py`, so the same commands can be run from the stack
directory.)

**How to install it.** Download and extract the release tarball into your coding
agent's skills directory (where it discovers
[Agent Skills](https://agentskills.io)); the
[`soliplex-skills` installation guide](https://soliplex.github.io/soliplex-skills/mechanisms/installation/)
covers fetching a published skill.

**Keeping it current.** Each published copy bundles `scripts/skill_versions.py`,
which can `list`, `diff`, and `upgrade` the installed skill against the published
builds — see the
[`soliplex-skills` versions CLI docs](https://soliplex.github.io/soliplex-skills/mechanisms/versions-cli/).
