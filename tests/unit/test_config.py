from soliplex_concierge import config


def test_gitc_ctor(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"

    git_config = config.GiteaIssueTargetToolConfig(
        _installation_config=installation_config,
        _config_path=config_path,
        owner="acme",
        repo="widgets",
    )

    assert git_config._installation_config is installation_config
    assert git_config._config_path == config_path
    assert git_config.kind == config.GITC_TOOL_KIND
    assert git_config.tool_name == config.GITC_TOOL_NAME
    assert git_config.get_extra_parameters() == {
        "owner": "acme",
        "repo": "widgets",
    }


def test_gitc_from_yaml(installation_config, temp_dir):
    config_path = temp_dir / "rooms" / "test" / "room_config.yaml"
    config_dict = {
        "tool_name": config.GITC_TOOL_NAME,
        "owner": "acme",
        "repo": "widgets",
    }

    git_config = config.GiteaIssueTargetToolConfig.from_yaml(
        installation_config=installation_config,
        config_path=config_path,
        config_dict=config_dict,
    )

    assert git_config._installation_config is installation_config
    assert git_config._config_path == config_path
    assert git_config.owner == "acme"
    assert git_config.repo == "widgets"
