from unittest import mock

import pytest
from soliplex import agents
from soliplex import installation

from soliplex_concierge import config
from soliplex_concierge.tools import gitea

USER = {
    "full_name": "Phreddy Phlyntstone",
    "email": "phreddy@example.com",
}


@pytest.fixture
def the_installation():
    return mock.create_autospec(installation.Installation)


@pytest.mark.anyio
async def test_get_gitea_issue_target(the_installation):
    tool_config = mock.Mock(owner="acme", repo="widgets")
    deps = agents.AgentDependencies(
        the_installation=the_installation,
        user=USER,
        tool_configs={config.GITC_TOOL_KIND: tool_config},
    )
    ctx = mock.Mock(spec_set=(["deps"]), deps=deps)

    found = await gitea.get_gitea_issue_target(ctx=ctx)

    assert isinstance(found, gitea.GiteaIssueTarget)
    assert found.owner == "acme"
    assert found.repo == "widgets"
