from unittest import mock

import httpx
import pytest
from soliplex import agents
from soliplex import models

from soliplex_concierge import config
from soliplex_concierge.tools import gitea

USER = models.UserProfile(
    given_name="Phreddy",
    family_name="Phlyntstone",
    email="phreddy@example.com",
    preferred_username="phreddy",
)


@pytest.mark.needs_docker
@pytest.mark.anyio
async def test_create_gitea_issue_against_live_gitea(gitea_server):
    tool_config = mock.Mock(
        owner=gitea_server["owner"],
        repo=gitea_server["repo"],
        host=gitea_server["host"],
        token=gitea_server["token"],
        profile_attachment_name=config.DEFAULT_PROFILE_ATTACHMENT_NAME,
        exclude_claims=[],
    )
    the_installation = mock.Mock()
    deps = agents.AgentDependencies(
        the_installation=the_installation,
        user=USER,
        tool_configs={config.CGI_TOOL_KIND: tool_config},
    )
    ctx = mock.Mock(spec_set=(["deps"]), deps=deps)

    issue = await gitea.create_gitea_issue(
        ctx=ctx,
        title="Room access request: marketing",
        body="Requested by Phreddy Phlyntstone.",
        request_type="room-access",
    )

    assert isinstance(issue, gitea.CreatedGiteaIssue)
    assert issue.number == 1
    assert issue.title == "Room access request: marketing"
    assert issue.url.endswith(
        f"/{gitea_server['owner']}/{gitea_server['repo']}/issues/1"
    )
    verify = httpx.get(
        f"{gitea_server['host']}/api/v1/repos"
        f"/{gitea_server['owner']}/{gitea_server['repo']}/issues/1",
        headers={"Authorization": f"token {gitea_server['token']}"},
        timeout=10,
    )
    assert verify.status_code == 200
    assert verify.json()["body"] == "Requested by Phreddy Phlyntstone."
    # the tool self-created the 'room-access' type label and applied it
    assert "room-access" in {
        label["name"] for label in verify.json()["labels"]
    }
    # the requesting user's profile was attached as a YAML file
    assets = httpx.get(
        f"{gitea_server['host']}/api/v1/repos"
        f"/{gitea_server['owner']}/{gitea_server['repo']}/issues/1/assets",
        headers={"Authorization": f"token {gitea_server['token']}"},
        timeout=10,
    )
    assert assets.status_code == 200
    assert {asset["name"] for asset in assets.json()} == {
        config.DEFAULT_PROFILE_ATTACHMENT_NAME
    }
