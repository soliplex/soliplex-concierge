import asyncio
import inspect
import traceback
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


def _patch_async_client(get_responses, post_responses):
    """Patch httpx.AsyncClient; get/post yield the given responses in order.

    The label lookup walks Gitea's paginated listing, so 'get_responses' is a
    sequence: one response per label page the lookup is expected to fetch.
    """
    client = mock.AsyncMock()
    client.get.side_effect = list(get_responses)
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
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_async_client([labels], [issue, asset])
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
    client.get.assert_awaited_once_with(
        f"{REPO}/labels",
        headers=HEADERS,
        params={"page": 1, "limit": gitea.LABEL_PAGE_SIZE},
    )
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
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": "user-profile.yaml.txt"})
    patched_client, client = _patch_async_client([labels], [issue, asset])

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
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_async_client([labels], [issue, asset])

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
            "labels": [{"name": "room-access"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    posts = [created, issue, asset]
    patched_client, client = _patch_async_client([labels], posts)

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="Room access request: chat",
            body="Requested by Phreddy.",
            request_type="room-access",
        )

    assert found.number == 8
    client.get.assert_awaited_once_with(
        f"{REPO}/labels",
        headers=HEADERS,
        params={"page": 1, "limit": gitea.LABEL_PAGE_SIZE},
    )
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
async def test_create_gitea_issue_reuses_label_from_a_later_page(ctx):
    # the canonical label sits past the first page of the label listing
    first_page = _resp(
        [
            {"name": f"filler-{n}", "id": n}
            for n in range(gitea.LABEL_PAGE_SIZE)
        ]
    )
    second_page = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_async_client(
        [first_page, second_page], [issue, asset]
    )

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    assert found.number == 7
    assert client.get.await_args_list == [
        mock.call(
            f"{REPO}/labels",
            headers=HEADERS,
            params={"page": page, "limit": gitea.LABEL_PAGE_SIZE},
        )
        for page in (1, 2)
    ]
    # the existing label was reused: no label was manufactured (issue #95)
    issue_call, _asset_call = client.post.await_args_list
    assert issue_call.kwargs["json"]["labels"] == [5]


@pytest.mark.anyio
async def test_create_gitea_issue_bounds_the_label_page_walk(ctx):
    # a server that ignored 'page' would otherwise be walked forever
    page = [{"name": f"filler-{n}", "id": n} for n in range(3)]
    pages = [_resp(page) for _ in range(gitea.MAX_LABEL_PAGES)]
    created = _resp({"name": "new-room", "id": 5})
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_async_client(
        pages, [created, issue, asset]
    )

    with patched_client:
        found = await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    assert found.number == 7
    assert client.get.await_count == gitea.MAX_LABEL_PAGES
    create_call, _issue_call, _asset_call = client.post.await_args_list
    assert create_call.args[0] == f"{REPO}/labels"


@pytest.mark.anyio
async def test_create_gitea_issue_rejects_unknown_request_type(ctx):
    patched_client, client = _patch_async_client([_resp([])], [])

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
    patched_client, client = _patch_async_client([_resp([])], [])

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
    patched_client, _client = _patch_async_client([labels], [issue])

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
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp(
        error=httpx.HTTPStatusError(
            "413 Payload Too Large", request=mock.Mock(), response=mock.Mock()
        )
    )
    patched_client, _client = _patch_async_client([labels], [issue, asset])

    with patched_client, pytest.raises(httpx.HTTPStatusError):
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )


@pytest.mark.anyio
async def test_create_gitea_issue_raises_when_label_dropped(ctx):
    labels = _resp([{"name": "room-access", "id": 9}])
    issue = _resp(
        {
            "number": 12,
            "html_url": f"{REPO}/issues/12",
            "title": "Room access request: chat",
            "labels": [],
        }
    )
    patched_client, client = _patch_async_client([labels], [issue])

    with patched_client, pytest.raises(gitea.LabelNotApplied) as exc:
        await gitea.create_gitea_issue(
            ctx=ctx,
            title="Room access request: chat",
            body="Requested by Phreddy.",
            request_type="room-access",
        )

    assert exc.value.request_type == "room-access"
    assert exc.value.number == 12
    assert exc.value.url == f"{REPO}/issues/12"
    assert "Write access" in str(exc.value)
    (issue_call,) = client.post.await_args_list
    assert issue_call.args[0] == f"{REPO}/issues"


SYNC_KWARGS = {
    "host": "https://gitea.example.com",
    "token": "tok-abc123",
    "owner": "acme",
    "repo": "widgets",
    "user": USER,
}


def _patch_sync_client(get_responses, post_responses):
    """Patch httpx.Client; get/post return the given responses in order."""
    client = mock.Mock()
    client.get.side_effect = list(get_responses)
    client.post.side_effect = list(post_responses)

    client_cm = mock.MagicMock()
    client_cm.__enter__.return_value = client
    client_cm.__exit__.return_value = False

    sync_client = mock.Mock(return_value=client_cm)
    patcher = mock.patch.object(gitea.httpx, "Client", sync_client)
    return patcher, client


@pytest.mark.parametrize(
    "user",
    [USER, USER.model_dump()],
    ids=["profile", "claims"],
)
def test_create_gitea_issue_sync_applies_existing_label(user):
    labels = _resp([{"name": "x", "id": 6}, {"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    patched_client, client = _patch_sync_client([labels], [issue, asset])
    patched_verify = mock.patch.object(gitea.tls, "httpx_verify")

    with (
        patched_verify as p_verify,
        patched_client as p_client,
    ):
        found = gitea.create_gitea_issue_sync(
            **(SYNC_KWARGS | {"user": user}),
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    assert found == gitea.CreatedGiteaIssue(
        number=7,
        url=f"{REPO}/issues/7",
        title="New room request: marketing",
    )
    p_client.assert_called_once_with(verify=p_verify.return_value)
    client.get.assert_called_once_with(
        f"{REPO}/labels",
        headers=HEADERS,
        params={"page": 1, "limit": gitea.LABEL_PAGE_SIZE},
    )
    issue_call, asset_call = client.post.call_args_list
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


def test_create_gitea_issue_sync_honors_profile_options():
    labels = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        {
            "number": 7,
            "html_url": f"{REPO}/issues/7",
            "title": "New room request: marketing",
            "labels": [{"name": "new-room"}],
        }
    )
    asset = _resp({"name": "user-profile.yaml.txt"})
    patched_client, client = _patch_sync_client([labels], [issue, asset])

    with patched_client:
        gitea.create_gitea_issue_sync(
            **SYNC_KWARGS,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
            profile_attachment_name="user-profile.yaml.txt",
            exclude_claims=["email"],
        )

    profile_yaml = (
        "given_name: Phreddy\n"
        "family_name: Phlyntstone\n"
        "preferred_username: phreddy\n"
    )
    _issue_call, asset_call = client.post.call_args_list
    assert asset_call == mock.call(
        f"{REPO}/issues/7/assets",
        headers=HEADERS,
        params={"name": "user-profile.yaml.txt"},
        files={
            "attachment": (
                "user-profile.yaml.txt",
                profile_yaml.encode("utf-8"),
                "application/x-yaml",
            )
        },
    )


def test_create_gitea_issue_sync_creates_missing_label():
    labels = _resp([])
    created = _resp({"name": "room-access", "id": 9})
    issue = _resp(
        {
            "number": 8,
            "html_url": f"{REPO}/issues/8",
            "title": "Room access request: chat",
            "labels": [{"name": "room-access"}],
        }
    )
    asset = _resp({"name": config.DEFAULT_PROFILE_ATTACHMENT_NAME})
    posts = [created, issue, asset]
    patched_client, client = _patch_sync_client([labels], posts)

    with patched_client:
        found = gitea.create_gitea_issue_sync(
            **SYNC_KWARGS,
            title="Room access request: chat",
            body="Requested by Phreddy.",
            request_type="room-access",
        )

    assert found.number == 8
    create_call, issue_call, _asset_call = client.post.call_args_list
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


def test_create_gitea_issue_sync_rejects_unknown_request_type():
    patched_client, client = _patch_sync_client([_resp([])], [])

    with patched_client, pytest.raises(gitea.UnknownRequestType) as exc:
        gitea.create_gitea_issue_sync(
            **SYNC_KWARGS,
            title="Mystery request",
            body="Requested by Phreddy.",
            request_type="bogus",
        )

    assert exc.value.request_type == "bogus"
    assert exc.value.supported == {"new-room", "room-access"}
    client.get.assert_not_called()


def test_create_gitea_issue_sync_raises_on_http_error():
    labels = _resp([{"name": "new-room", "id": 5}])
    issue = _resp(
        error=httpx.HTTPStatusError(
            "401 Unauthorized", request=mock.Mock(), response=mock.Mock()
        )
    )
    patched_client, _client = _patch_sync_client([labels], [issue])

    with patched_client, pytest.raises(httpx.HTTPStatusError):
        gitea.create_gitea_issue_sync(
            **SYNC_KWARGS,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )


def test_create_gitea_issue_sync_raises_when_label_dropped():
    labels = _resp([{"name": "room-access", "id": 9}])
    issue = _resp(
        {
            "number": 12,
            "html_url": f"{REPO}/issues/12",
            "title": "Room access request: chat",
            "labels": [],
        }
    )
    patched_client, client = _patch_sync_client([labels], [issue])

    with patched_client, pytest.raises(gitea.LabelNotApplied) as exc:
        gitea.create_gitea_issue_sync(
            **SYNC_KWARGS,
            title="Room access request: chat",
            body="Requested by Phreddy.",
            request_type="room-access",
        )

    assert exc.value.number == 12
    assert exc.value.url == f"{REPO}/issues/12"
    (issue_call,) = client.post.call_args_list
    assert issue_call.args[0] == f"{REPO}/issues"


CONNECT_ERROR = httpx.ConnectError("connection refused")


def _observed_steps(observer):
    """One-request steps reporting to 'observer' whatever ends them early."""
    try:
        yield ("get", f"{REPO}/labels", {"headers": HEADERS})
    except BaseException as exc:
        observer(exc)
        raise


@pytest.mark.parametrize(
    "error, seen_type",
    [
        (CONNECT_ERROR, httpx.ConnectError),
        (KeyboardInterrupt(), GeneratorExit),
    ],
    ids=["thrown", "closed"],
)
def test_drive_sync_ends_steps_on_request_error(error, seen_type):
    observer = mock.Mock()
    steps = _observed_steps(observer)
    client = mock.Mock()
    client.get.side_effect = error

    with pytest.raises(type(error)):
        gitea._drive_sync(client, steps)

    (seen,), _ = observer.call_args
    assert isinstance(seen, seen_type)
    assert inspect.getgeneratorstate(steps) == inspect.GEN_CLOSED


@pytest.mark.parametrize(
    "error, seen_type",
    [
        (CONNECT_ERROR, httpx.ConnectError),
        (asyncio.CancelledError(), GeneratorExit),
    ],
    ids=["thrown", "closed"],
)
@pytest.mark.anyio
async def test_drive_async_ends_steps_on_request_error(error, seen_type):
    observer = mock.Mock()
    steps = _observed_steps(observer)
    client = mock.AsyncMock()
    client.get.side_effect = error

    with pytest.raises(type(error)):
        await gitea._drive_async(client, steps)

    (seen,), _ = observer.call_args
    assert isinstance(seen, seen_type)
    assert inspect.getgeneratorstate(steps) == inspect.GEN_CLOSED


def test_create_gitea_issue_sync_traces_request_error_to_its_step():
    patched_client, _client = _patch_sync_client([CONNECT_ERROR], [])

    with patched_client, pytest.raises(httpx.ConnectError) as exc:
        gitea.create_gitea_issue_sync(
            **SYNC_KWARGS,
            title="New room request: marketing",
            body="Requested by Phreddy.",
            request_type="new-room",
        )

    # the step that asked for the request sits between the driver and the
    # client call that raised
    names = [frame.name for frame in traceback.extract_tb(exc.tb)]
    start = names.index("_drive_sync") + 1
    assert names[start : start + 3] == [
        "_file_issue",
        "_ensure_label",
        "_find_label",
    ]
