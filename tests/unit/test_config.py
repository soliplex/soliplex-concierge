from soliplex_concierge import config


def test_cgi_ctor(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"

    git_config = config.CreateGiteaIssueToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        owner="acme",
        repo="widgets",
    )

    assert git_config._installation_config is installation_config
    assert git_config._config_path == config_path
    assert git_config.kind == config.CGI_TOOL_KIND
    assert git_config.tool_name == config.CGI_TOOL_NAME
    assert git_config.host_env_var == "GITEA_HOST"
    assert git_config.token_secret_name == "GITEA_ACCESS_TOKEN"
    assert git_config.get_extra_parameters() == {
        "owner": "acme",
        "repo": "widgets",
        "host_env_var": "GITEA_HOST",
        "token_secret_name": "GITEA_ACCESS_TOKEN",
    }


def test_cgi_from_yaml(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    config_dict = {
        "tool_name": config.CGI_TOOL_NAME,
        "owner": "acme",
        "repo": "widgets",
        "host_env_var": "GH_HOST",
        "token_secret_name": "GH_TOKEN",
    }

    git_config = config.CreateGiteaIssueToolConfig.from_yaml(
        installation_config=installation_config,
        config_path=config_path,
        config_dict=config_dict,
    )

    assert git_config._installation_config is installation_config
    assert git_config._config_path == config_path
    assert git_config.owner == "acme"
    assert git_config.repo == "widgets"
    assert git_config.host_env_var == "GH_HOST"
    assert git_config.token_secret_name == "GH_TOKEN"
