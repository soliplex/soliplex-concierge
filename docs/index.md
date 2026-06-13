# Soliplex Concierge

`soliplex-concierge` is a [Soliplex](https://github.com/soliplex/soliplex)
extension that supports LLM-driven, on-demand creation of issues tracking
requested changes to a Soliplex installation's configuration:

- Access to a non-public room
- Creation of a new room

## Components

- The **Python library** ([`src/soliplex_concierge`](https://github.com/soliplex/soliplex-concierge/tree/main/src/soliplex_concierge))
  contains code to support both the issue-creation tasks and the scripts which
  actually perform the requested updates.

- The **agent skills** ([`skills/`](https://github.com/soliplex/soliplex-concierge/tree/main/skills))
  contain [skill definitions](https://agentskills.io) which allow an agent to
  perform these tasks:

  - `soliplex-concierge-installer` — wire the extension into a stack
  - `soliplex-concierge-room` — the in-room request formatter, copied into
    the stack
  - `soliplex-concierge-admin` — act on and resolve filed requests

## In this documentation

- [Installation & wiring](installation.md) — add the extension to a stack,
  either via the one-shot installer or by hand.
- [Skills & releases](skills.md) — the three bundled skills and how they are
  published.
- [Administration & status](administration.md) — resolving filed requests and
  the current implementation status.
