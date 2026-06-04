from unittest import mock

import httpx
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
    the_installation = mock.create_autospec(installation.Installation)
    the_installation.get_environment.return_value = "https://gitea.example.com"
    the_installation.get_secret.return_value = "tok-abc123"
    return the_installation


@pytest.fixture
def ctx(the_installation):
    tool_config = mock.Mock(
        owner="acme",
        repo="widgets",
        host_env_var="GITEA_HOST",
        token_secret_name="GITEA_ACCESS_TOKEN",
    )
    deps = agents.AgentDependencies(
        the_installation=the_installation,
        user=USER,
        tool_configs={config.CGI_TOOL_KIND: tool_config},
    )
    return mock.Mock(spec_set=(["deps"]), deps=deps)


def _patch_async_client(response):
    client = mock.AsyncMock()
    client.post.return_value = response

    client_cm = mock.AsyncMock()
    client_cm.__aenter__.return_value = client
    client_cm.__aexit__.return_value = False

    async_client = mock.Mock(return_value=client_cm)
    return mock.patch.object(gitea.httpx, "AsyncClient", async_client), client


@pytest.mark.anyio
async def test_create_gitea_issue(ctx, the_installation):
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = {
        "number": 7,
        "html_url": "https://gitea.example.com/acme/widgets/issues/7",
        "title": "New room request: marketing",
    }
    patched_client, client = _patch_async_client(response)

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
        )

    assert isinstance(found, gitea.CreatedGiteaIssue)
    assert found.number == 7
    assert found.url == "https://gitea.example.com/acme/widgets/issues/7"
    assert found.title == "New room request: marketing"
    the_installation.get_environment.assert_called_once_with("GITEA_HOST")
    the_installation.get_secret.assert_called_once_with("GITEA_ACCESS_TOKEN")
    client.post.assert_awaited_once_with(
        "https://gitea.example.com/api/v1/repos/acme/widgets/issues",
        headers={"Authorization": "token tok-abc123"},
        json={
            "title": "New room request: marketing",
            "body": "Requested by Phreddy.",
        },
    )


@pytest.mark.anyio
async def test_create_gitea_issue_raises_on_http_error(ctx):
    response = mock.Mock()
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=mock.Mock(),
        response=mock.Mock(),
    )
    patched_client, _client = _patch_async_client(response)

    with patched_client, pytest.raises(httpx.HTTPStatusError):
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
        )
