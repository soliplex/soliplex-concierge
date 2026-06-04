"""Tool configuration types for the 'soliplex-concierge' extension.

Register 'CreateGiteaIssueToolConfig' with a Soliplex installation by listing
it under the installation's 'meta.tool_configs', e.g.::

    meta:
      tool_configs:
        - "soliplex_concierge.config.CreateGiteaIssueToolConfig"

Soliplex then maps the 'tool_name' in a room's 'tools:' entry to this class.
"""

import dataclasses

from soliplex.config import tools as config_tools

CGI_TOOL_NAME = "soliplex_concierge.tools.gitea.create_gitea_issue"
_, CGI_TOOL_KIND = CGI_TOOL_NAME.rsplit(".", 1)


@dataclasses.dataclass(kw_only=True)
class CreateGiteaIssueToolConfig(config_tools.ToolConfig):
    kind: str = CGI_TOOL_KIND
    tool_name: str = CGI_TOOL_NAME

    # The repository room requests are filed against.
    owner: str
    repo: str

    # Where the tool reads the Gitea base URL and access token at runtime:
    # 'host_env_var' names an installation environment variable, and
    # 'token_secret_name' names an installation secret.
    host_env_var: str = "GITEA_HOST"
    token_secret_name: str = "GITEA_ACCESS_TOKEN"

    def get_extra_parameters(self) -> dict:
        local = {
            "owner": self.owner,
            "repo": self.repo,
            "host_env_var": self.host_env_var,
            "token_secret_name": self.token_secret_name,
        }
        return super().get_extra_parameters() | local
