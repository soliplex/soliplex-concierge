"""Tool configuration types for the 'soliplex-concierge' extension.

Register 'CreateGiteaIssueToolConfig' with a Soliplex installation by listing
it under the installation's 'meta.tool_configs', e.g.::

    meta:
      tool_configs:
        - "soliplex_concierge.config.CreateGiteaIssueToolConfig"

Soliplex then maps the 'tool_name' in a room's 'tools:' entry to this class.
"""

import dataclasses
import pathlib
import typing

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

    # The Gitea base URL and access token, as Soliplex interpolation strings:
    # '_host' carries an 'env:' marker and '_token' a 'secret:' marker. The
    # 'host' / 'token' properties resolve them lazily at tool-call time (the
    # installation's environment and secrets are not resolved until after room
    # configs -- and thus tool configs -- are constructed).
    _host: str = "env:GITEA_HOST"
    _token: str = "secret:GITEA_ACCESS_TOKEN"

    @property
    def host(self) -> str:
        return self._installation_config.interpolate_environment(self._host)

    @property
    def token(self) -> str:
        return self._installation_config.interpolate_secrets(self._token)

    @classmethod
    def from_yaml(
        cls,
        installation_config,
        config_path: pathlib.Path,
        config_dict: dict[str, typing.Any],
    ):
        if "host" in config_dict:
            config_dict["_host"] = config_dict.pop("host")
        if "token" in config_dict:
            config_dict["_token"] = config_dict.pop("token")
        return super().from_yaml(
            installation_config=installation_config,
            config_path=config_path,
            config_dict=config_dict,
        )

    def get_extra_parameters(self) -> dict:
        local = {
            "owner": self.owner,
            "repo": self.repo,
            "host": self._host,
            "token": self._token,
        }
        return super().get_extra_parameters() | local
