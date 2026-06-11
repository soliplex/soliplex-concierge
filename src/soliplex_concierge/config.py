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

# Filename for the requesting user's profile, attached to every issue. Gitea
# only accepts attachments whose extension is in its '[attachment]
# ALLOWED_TYPES' list; '.yaml' is allowed by the production server but not by a
# stock Gitea, so the name is configurable per room via
# 'profile_attachment_name'.
DEFAULT_PROFILE_ATTACHMENT_NAME = "user-profile.yaml"


@dataclasses.dataclass(kw_only=True)
class CreateGiteaIssueToolConfig(config_tools.ToolConfig):
    kind: str = CGI_TOOL_KIND
    tool_name: str = CGI_TOOL_NAME

    # The repository room requests are filed against. '_owner' / '_repo' may
    # carry Soliplex interpolation markers (e.g. 'env:GITEA_OWNER'); the
    # 'owner' / 'repo' properties resolve them lazily at tool-call time, just
    # like 'host' below.
    _owner: str
    _repo: str

    # The Gitea base URL and access token, as Soliplex interpolation strings:
    # '_host' carries an 'env:' marker and '_token' a 'secret:' marker. The
    # 'host' / 'token' properties resolve them lazily at tool-call time (the
    # installation's environment and secrets are not resolved until after room
    # configs -- and thus tool configs -- are constructed).
    _host: str = "env:GITEA_HOST"
    _token: str = "secret:GITEA_ACCESS_TOKEN"

    # Name of the YAML file the requesting user's profile is attached under.
    # The extension must be one Gitea's '[attachment] ALLOWED_TYPES' permits.
    profile_attachment_name: str = DEFAULT_PROFILE_ATTACHMENT_NAME

    # Fields to be dropped from the user profile before converting to
    # YAML for the issue attachment.
    exclude_claims: list[str] = dataclasses.field(default_factory=list)

    @property
    def owner(self) -> str:
        return self._installation_config.interpolate_environment(self._owner)

    @property
    def repo(self) -> str:
        return self._installation_config.interpolate_environment(self._repo)

    @property
    def host(self) -> str:
        return self._installation_config.interpolate_environment(self._host)

    @property
    def token(self) -> str:
        return self._installation_config.interpolate_secrets(self._token)

    # Public 'config_dict' keys mapped to the private fields backing the
    # lazily-interpolated 'owner' / 'repo' / 'host' / 'token' properties.
    _FIELD_ALIASES = {
        "owner": "_owner",
        "repo": "_repo",
        "host": "_host",
        "token": "_token",
    }

    @classmethod
    def from_yaml(
        cls,
        installation_config,
        config_path: pathlib.Path,
        config_dict: dict[str, typing.Any],
    ):
        for public, private in cls._FIELD_ALIASES.items():
            if public in config_dict:
                config_dict[private] = config_dict.pop(public)
        return super().from_yaml(
            installation_config=installation_config,
            config_path=config_path,
            config_dict=config_dict,
        )

    def get_extra_parameters(self) -> dict:
        local = {
            "owner": self._owner,
            "repo": self._repo,
            "host": self._host,
            "token": self._token,
            "profile_attachment_name": self.profile_attachment_name,
            "exclude_claims": self.exclude_claims,
        }
        return super().get_extra_parameters() | local
