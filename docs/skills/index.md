# Skills

The concierge is delivered as three [Agent Skills](https://agentskills.io),
each published as a release artifact and installed either into a coding agent or
into a Soliplex room. Each page below covers what the skill is for, where to find
it, and where and how to install it:

- **[`soliplex-concierge-installer`](soliplex-concierge-installer.md)** — wires
  the extension into a stack; runs in a coding agent.
- **[`soliplex-concierge-room`](soliplex-concierge-room.md)** — the in-room
  request formatter; installed into a room's configuration.
- **[`soliplex-concierge-admin`](soliplex-concierge-admin.md)** — resolves the
  filed requests; runs in a coding agent.

## Related skills

The concierge works alongside two skills published by sibling repositories.
Their own documentation covers installing and using them:

- **[`soliplex-template`](https://soliplex.github.io/soliplex-template/)** —
  generate and configure a Soliplex stack, create rooms, and build RAG
  databases. The installer uses it to stand up a stack, and the admin uses it to
  provision the rooms requests ask for.
- **[`soliplex-docs`](https://soliplex.github.io/soliplex/docs-skill/)** — the
  full Soliplex documentation as a skill. The installer wires it into the
  about-room so the concierge can answer "how do I …" questions about Soliplex,
  and the admin consults it for room, access, and RAG configuration.
