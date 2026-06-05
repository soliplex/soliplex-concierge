from unittest import mock

import httpx
import pytest
from soliplex import agents

from soliplex_concierge import config
from soliplex_concierge.tools import gitea

USER = {
    "full_name": "Phreddy Phlyntstone",
    "email": "phreddy@example.com",
}


@pytest.mark.needs_docker
@pytest.mark.anyio
async def test_create_gitea_issue_against_live_gitea(gitea_server):
    tool_config = mock.Mock(
        owner=gitea_server["owner"],
        repo=gitea_server["repo"],
        host=gitea_server["host"],
        token=gitea_server["token"],
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
