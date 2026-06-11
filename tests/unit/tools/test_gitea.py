from unittest import mock

import httpx
import pytest
from soliplex import agents
from soliplex import installation
from soliplex import models

from soliplex_concierge import config
from soliplex_concierge.tools import gitea

USER = models.UserProfile(
    given_name="Phreddy",
    family_name="Phlyntstone",
    email="phreddy@example.com",
    preferred_username="phreddy",
)

REPO = "https://gitea.example.com/api/v1/repos/acme/widgets"
HEADERS = {"Authorization": "token tok-abc123"}

# YAML the user profile serializes to, in field-declaration order.
PROFILE_YAML = (
    "given_name: Phreddy\n"
    "family_name: Phlyntstone\n"
    "email: phreddy@example.com\n"
    "preferred_username: phreddy\n"
)


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
        profile_attachment_name=config.DEFAULT_PROFILE_ATTACHMENT_NAME,
        exclude_claims=[],
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
    patcher = mock.patch.object(gitea.httpx, "AsyncClient", async_client)
    return patcher, client


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
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_async_client(labels, [issue, asset])
    patched_verify = mock.patch.object(gitea.tls, "httpx_verify")

    with (
        patched_verify as p_verify,
        patched_client as p_client,
    ):
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

    p_client.assert_called_once_with(verify=p_verify.return_value)
    client.get.assert_awaited_once_with(f"{REPO}/labels", headers=HEADERS)
    issue_call, asset_call = client.post.await_args_list
    assert issue_call == mock.call(
        f"{REPO}/issues",
        headers=HEADERS,
        json={
            "title": "New room request: marketing",
            "body": "Requested by Phreddy.",
            "labels": [5],
        },
    )
    assert asset_call == mock.call(
        f"{REPO}/issues/7/assets",
        headers=HEADERS,
        params={"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME},
        files={
            "attachment": (
                config.DEFAULT_PROFILE_ATTACHMENT_NAME,
                PROFILE_YAML.encode("utf-8"),
                "application/x-yaml",
            )
        },
    )


@pytest.mark.anyio
async def test_create_gitea_issue_honors_configured_attachment_name(ctx):
    ctx.deps.tool_configs[
        config.CGI_TOOL_KIND
    ].profile_attachment_name = "user-profile.yaml.txt"
    labels = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
        }
    )
    asset = _resp({"name": "user-profile.yaml.txt"})
    patched_client, client = _patch_async_client(labels, [issue, asset])

    with patched_client:
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    _issue_call, asset_call = client.post.await_args_list
    assert asset_call == mock.call(
        f"{REPO}/issues/7/assets",
        headers=HEADERS,
        params={"name": "user-profile.yaml.txt"},
        files={
            "attachment": (
                "user-profile.yaml.txt",
                PROFILE_YAML.encode("utf-8"),
                "application/x-yaml",
            )
        },
    )


@pytest.mark.anyio
async def test_create_gitea_issue_excludes_configured_claims(ctx):
    ctx.deps.tool_configs[config.CGI_TOOL_KIND].exclude_claims = ["email"]
    labels = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_async_client(labels, [issue, asset])

    with patched_client:
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    profile_yaml = (
        "given_name: Phreddy\n"
        "family_name: Phlyntstone\n"
        "preferred_username: phreddy\n"
    )
    _issue_call, asset_call = client.post.await_args_list
    assert asset_call == mock.call(
        f"{REPO}/issues/7/assets",
        headers=HEADERS,
        params={"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME},
        files={
            "attachment": (
                config.DEFAULT_PROFILE_ATTACHMENT_NAME,
                profile_yaml.encode("utf-8"),
                "application/x-yaml",
            )
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
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    posts = [created, issue, asset]
    patched_client, client = _patch_async_client(labels, posts)

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="Room access request: chat",
            body="Requested by Phreddy.",
            request_type="room-access",
        )

    assert found.number == 8
    create_call, issue_call, _asset_call = client.post.await_args_list
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
async def test_create_gitea_issue_rejects_unknown_request_type(ctx):
    patched_client, client = _patch_async_client(_resp([]), [])

    with patched_client, pytest.raises(gitea.UnknownRequestType) as exc:
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="Mystery request",
            body="Requested by Phreddy.",
            request_type="bogus",
        )

    assert exc.value.request_type == "bogus"
    assert exc.value.supported == {"new-room", "room-access"}
    assert "new-room" in str(exc.value)
    client.get.assert_not_awaited()


@pytest.mark.anyio
async def test_create_gitea_issue_rejects_anonymous_user(ctx):
    ctx.deps.user = None
    patched_client, client = _patch_async_client(_resp([]), [])

    with patched_client, pytest.raises(gitea.AnonymousUser) as exc:
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested anonymously.",
            request_type="new-room",
        )

    assert "anonymous" in str(exc.value)
    client.get.assert_not_awaited()


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


@pytest.mark.anyio
async def test_create_gitea_issue_raises_on_attachment_error(ctx):
    labels = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
        }
    )
    asset = _resp(
        error=httpx.HTTPStatusError(
            "413 Payload Too Large", request=mock.Mock(), response=mock.Mock()
        )
    )
    patched_client, _client = _patch_async_client(labels, [issue, asset])

    with patched_client, pytest.raises(httpx.HTTPStatusError):
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )
