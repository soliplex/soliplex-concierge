# Skills & releases

Each skill under [`skills/`](https://github.com/soliplex/soliplex-concierge/tree/main/skills)
is published from CI
([`build-skills.yaml`](https://github.com/soliplex/soliplex-concierge/blob/main/.github/workflows/build-skills.yaml))
as a standalone GitHub release artifact, following the pattern `soliplex` uses
for its skills:

- **Rolling builds** — every change to `skills/**` on `main` publishes an
  immutable `<prefix>-YYYY.MM.DD-<sha>` prerelease (prefixes: `installer-skill`,
  `room-skill`, `admin-skill`) and updates a `<prefix>-latest` pointer; the ten
  newest rolling builds per skill are kept.
- **Tagged releases** — publishing a software release (`v*`) attaches all three
  skill tarballs to that release, pinned to the version.

Each published skill bundles `scripts/skill_versions.py`, so an installed copy
can `list`, `diff`, and `upgrade` itself against the published builds, e.g.
`python scripts/skill_versions.py list`.
