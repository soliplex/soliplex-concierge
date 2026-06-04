"""Tool configuration types for the 'soliplex-concierge' extension.

Register 'GiteaIssueTargetToolConfig' with a Soliplex installation by listing
it under the installation's 'meta.tool_configs', e.g.::

    meta:
      tool_configs:
        - "soliplex_concierge.config.GiteaIssueTargetToolConfig"

Soliplex then maps the 'tool_name' in a room's 'tools:' entry to this class.
"""

import dataclasses

from soliplex.config import tools as config_tools

GITC_TOOL_NAME = "soliplex_concierge.tools.gitea.get_gitea_issue_target"
_, GITC_TOOL_KIND = GITC_TOOL_NAME.rsplit(".", 1)


@dataclasses.dataclass(kw_only=True)
class GiteaIssueTargetToolConfig(config_tools.ToolConfig):
    kind: str = GITC_TOOL_KIND
    tool_name: str = GITC_TOOL_NAME

    owner: str
    repo: str

    def get_extra_parameters(self) -> dict:
        local = {
            "owner": self.owner,
            "repo": self.repo,
        }
        return super().get_extra_parameters() | local
