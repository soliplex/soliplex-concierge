# `soliplex-concierge`: room access / creation support

This project provides a [Soliplex](https://github.com/soliplex/soliplex)
extension to support LLM-driven, on-demand creation of issues tracking
requested changes to a Soliplex installation's configuration:

- Access to a non-public room
- Creation of a new room

Components:

- The [Python library](src/soliplex_concierge) contains code to support
  both the issue creation tasks and the scripts which actually perform
  the requested updates.

- The [agent skill](skill/) contains a
  [skill definition](https://agentskills.io) which allows an agent to
  perform these tasks.

## Install & wire up

1. Install this package into the environment that runs Soliplex (it depends
   on `soliplex`):

   ```sh
   pip install -e .
   ```

2. Merge the entries from
   [`example/installation-snippet.yaml`](example/installation-snippet.yaml)
   into your `installation.yaml`. They:

   - register the tool-config class via `meta.tool_configs` (Soliplex
     resolves the tool by its dotted `tool_name`; no core edit is needed),
   - add this package's `skill/` directory to `filesystem_skills_paths` and
     enable the `soliplex-concierge` filesystem skill, and
   - declare the `GITEA_HOST` environment variable and `GITEA_ACCESS_TOKEN`
     secret used by the room's `gitea` MCP toolset.

3. Copy [`example/rooms/about_soliplex/`](example/rooms/about_soliplex) into
   your installation's `rooms/` directory, editing the `owner` / `repo` on
   the `get_gitea_issue_target` tool to point at your tracking repository.

4. Set `GITEA_HOST` and `GITEA_ACCESS_TOKEN` (see the `.env` lines at the
   bottom of the installation snippet).

## Status

The issue-filing concierge (the `about_soliplex` room, its `gitea` tool, and
the agent skill) is implemented. The scripts that *act on* approved requests
— actually creating rooms and granting room access — are a planned
follow-up.
