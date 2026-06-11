from soliplex_concierge import config


def test_cgi_ctor(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"

    git_config = config.CreateGiteaIssueToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        _owner="acme",
        _repo="widgets",
    )

    assert git_config._installation_config is installation_config
    assert git_config._config_path == config_path
    assert git_config.kind == config.CGI_TOOL_KIND
    assert git_config.tool_name == config.CGI_TOOL_NAME
    assert git_config._owner == "acme"
    assert git_config._repo == "widgets"
    assert git_config._host == "env:GITEA_HOST"
    assert git_config._token == "secret:GITEA_ACCESS_TOKEN"
    assert git_config.profile_attachment_name == "user-profile.yaml"
    assert git_config.exclude_claims == []
    assert git_config.get_extra_parameters() == {
        "owner": "acme",
        "repo": "widgets",
        "host": "env:GITEA_HOST",
        "token": "secret:GITEA_ACCESS_TOKEN",
        "profile_attachment_name": "user-profile.yaml",
        "exclude_claims": [],
    }


def test_cgi_from_yaml(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    config_dict = {
        "tool_name": config.CGI_TOOL_NAME,
        "owner": "env:GH_OWNER",
        "repo": "env:GH_REPO",
        "host": "env:GH_HOST",
        "token": "secret:GH_TOKEN",
    }

    git_config = config.CreateGiteaIssueToolConfig.from_yaml(
        installation_config=installation_config,
        config_path=config_path,
        config_dict=config_dict,
    )

    assert git_config._installation_config is installation_config
    assert git_config._config_path == config_path
    assert git_config._owner == "env:GH_OWNER"
    assert git_config._repo == "env:GH_REPO"
    assert git_config._host == "env:GH_HOST"
    assert git_config._token == "secret:GH_TOKEN"


def test_cgi_from_yaml_accepts_profile_attachment_name(
    installation_config, temp_dir
):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    config_dict = {
        "tool_name": config.CGI_TOOL_NAME,
        "owner": "acme",
        "repo": "widgets",
        "profile_attachment_name": "profile.json",
    }

    git_config = config.CreateGiteaIssueToolConfig.from_yaml(
        installation_config=installation_config,
        config_path=config_path,
        config_dict=config_dict,
    )

    assert git_config.profile_attachment_name == "profile.json"


def test_cgi_from_yaml_accepts_exclude_claims(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    config_dict = {
        "tool_name": config.CGI_TOOL_NAME,
        "owner": "acme",
        "repo": "widgets",
        "exclude_claims": ["email"],
    }

    git_config = config.CreateGiteaIssueToolConfig.from_yaml(
        installation_config=installation_config,
        config_path=config_path,
        config_dict=config_dict,
    )

    assert git_config.exclude_claims == ["email"]


def test_cgi_from_yaml_without_host_token_keeps_defaults(
    installation_config, temp_dir
):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    config_dict = {
        "tool_name": config.CGI_TOOL_NAME,
        "owner": "acme",
        "repo": "widgets",
    }

    git_config = config.CreateGiteaIssueToolConfig.from_yaml(
        installation_config=installation_config,
        config_path=config_path,
        config_dict=config_dict,
    )

    assert git_config._host == "env:GITEA_HOST"
    assert git_config._token == "secret:GITEA_ACCESS_TOKEN"


def test_cgi_owner_interpolates_environment(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    installation_config.interpolate_environment.return_value = "acme"
    git_config = config.CreateGiteaIssueToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        _owner="env:GITEA_OWNER",
        _repo="widgets",
    )

    owner = git_config.owner

    assert owner == "acme"
    installation_config.interpolate_environment.assert_called_once_with(
        "env:GITEA_OWNER"
    )


def test_cgi_repo_interpolates_environment(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    installation_config.interpolate_environment.return_value = "widgets"
    git_config = config.CreateGiteaIssueToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        _owner="acme",
        _repo="env:GITEA_REPO",
    )

    repo = git_config.repo

    assert repo == "widgets"
    installation_config.interpolate_environment.assert_called_once_with(
        "env:GITEA_REPO"
    )


def test_cgi_host_interpolates_environment(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    installation_config.interpolate_environment.return_value = (
        "https://gitea.example.com"
    )
    git_config = config.CreateGiteaIssueToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        _owner="acme",
        _repo="widgets",
    )

    host = git_config.host

    assert host == "https://gitea.example.com"
    installation_config.interpolate_environment.assert_called_once_with(
        "env:GITEA_HOST"
    )


def test_cgi_token_interpolates_secrets(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    installation_config.interpolate_secrets.return_value = "tok-abc123"
    git_config = config.CreateGiteaIssueToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        _owner="acme",
        _repo="widgets",
    )

    token = git_config.token

    assert token == "tok-abc123"
    installation_config.interpolate_secrets.assert_called_once_with(
        "secret:GITEA_ACCESS_TOKEN"
    )
