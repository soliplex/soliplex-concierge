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

REPO = "https://gitea.example.com/api/v1/repos/acme/widgets"
HEADERS = {"Authorization": "token tok-abc123"}


@pytest.fixture
def the_installation():
    return mock.create_autospec(installation.Installation)


@pytest.fixture
def ctx(the_installation):
    tool_config = mock.Mock(
        owner="acme",
        repo="widgets",
        host="https://gitea.example.com",
        token="tok-abc123",
    )
    deps = agents.AgentDependencies(
        the_installation=the_installation,
        user=USER,
        tool_configs={config.CGI_TOOL_KIND: tool_config},
    )
    return mock.Mock(spec_set=(["deps"]), deps=deps)


def _resp(payload=None, error=None):
    response = mock.Mock()
    if error is not None:
        response.raise_for_status.side_effect = error
    else:
        response.raise_for_status = mock.Mock()
    response.json.return_value = payload
    return response


def _patch_async_client(get_response, post_responses):
    client = mock.AsyncMock()
    client.get.return_value = get_response
    client.post.side_effect = list(post_responses)

    client_cm = mock.AsyncMock()
    client_cm.__aenter__.return_value = client
    client_cm.__aexit__.return_value = False

    async_client = mock.Mock(return_value=client_cm)
    return mock.patch.object(gitea.httpx, "AsyncClient", async_client), client


@pytest.mark.anyio
async def test_create_gitea_issue_applies_existing_label(ctx):
    labels = _resp([{"name": "x", "id": 6}, {"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
        }
    )
    patched_client, client = _patch_async_client(labels, [issue])

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    assert isinstance(found, gitea.CreatedGiteaIssue)
    assert found.number == 7
    assert found.url == f"{REPO}/issues/7"
    assert found.title == "New room request: marketing"
    client.get.assert_awaited_once_with(f"{REPO}/labels", headers=HEADERS)
    client.post.assert_awaited_once_with(
        f"{REPO}/issues",
        headers=HEADERS,
        json={
            "title": "New room request: marketing",
            "body": "Requested by Phreddy.",
            "labels": [5],
        },
    )


@pytest.mark.anyio
async def test_create_gitea_issue_creates_missing_label(ctx):
    labels = _resp([])
    created = _resp({"name": "room-access", "id": 9})
    issue = _resp(
        {
            "number": 8,
            "html_url": f"{REPO}/issues/8",
            "title": "Room access request: chat",
        }
    )
    patched_client, client = _patch_async_client(labels, [created, issue])

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="Room access request: chat",
            body="Requested by Phreddy.",
            request_type="room-access",
        )

    assert found.number == 8
    create_call, issue_call = client.post.await_args_list
    assert create_call == mock.call(
        f"{REPO}/labels",
        headers=HEADERS,
        json={
            "name": "room-access",
            "color": "#5319e7",
            "description": "Access request for an existing room",
        },
    )
    assert issue_call.kwargs["json"]["labels"] == [9]


@pytest.mark.anyio
async def test_create_gitea_issue_raises_on_http_error(ctx):
    labels = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        error=httpx.HTTPStatusError(
            "401 Unauthorized", request=mock.Mock(), response=mock.Mock()
        )
    )
    patched_client, _client = _patch_async_client(labels, [issue])

    with patched_client, pytest.raises(httpx.HTTPStatusError):
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )
