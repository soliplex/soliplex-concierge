# `soliplex-concierge`: room access / creation support

This project provides a [Soliplex](https://github.com/soliplex/soliplex)
extension to support LLM-driven, on-demand creation of issues tracking
requested changes to a Soliplex installation's configuration:

- Access to a non-public room
- Creation of a new room

Compoonents:

- The [Python library](src/soliplex_concierge) contains code to support
  both the issue creation tasks and the scripts which actually perform
  the requested updates.

- The [agent skill](skill/) contains a
  [skill definition](https://agentskills.io) which allows an agent to
  perform these tasks.
