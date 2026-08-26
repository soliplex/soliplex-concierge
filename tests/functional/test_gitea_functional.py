from unittest import mock

import httpx
import pytest
from soliplex import agents
from soliplex import models

from soliplex_concierge import config
from soliplex_concierge import gitea_admin
from soliplex_concierge.tools import gitea

USER = models.UserProfile(
    given_name="Phreddy",
    family_name="Phlyntstone",
    email="phreddy@example.com",
    preferred_username="phreddy",
)


@pytest.fixture
def ctx(gitea_server):
    """A run context wired to the throwaway server's repo and token."""
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
    return mock.Mock(spec_set=(["deps"]), deps=deps)


def _repo_api(gitea_server):
    return (
        f"{gitea_server['host']}/api/v1/repos"
        f"/{gitea_server['owner']}/{gitea_server['repo']}"
    )


@pytest.mark.needs_docker
@pytest.mark.anyio
async def test_create_gitea_issue_against_live_gitea(gitea_server, ctx):
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


@pytest.mark.needs_docker
@pytest.mark.anyio
async def test_create_gitea_issue_reuses_a_label_past_the_first_page(
    gitea_server, ctx
):
    """A canonical label off the first page is reused, not duplicated.

    Gitea caps its label listing at one page per request and happily accepts
    a second label carrying an existing name, so a lookup that stopped at the
    first page would manufacture a duplicate (issue #95).
    """
    repo_api = _repo_api(gitea_server)
    headers = {"Authorization": f"token {gitea_server['token']}"}
    # Seed the filler labels first and the canonical one last, so it lands
    # past the first page whether Gitea orders labels by name or by creation.
    for filler in range(gitea.LABEL_PAGE_SIZE + 10):
        response = httpx.post(
            f"{repo_api}/labels",
            headers=headers,
            json={"name": f"aaa-filler-{filler:03d}", "color": "#cccccc"},
            timeout=10,
        )
        response.raise_for_status()
    spec = gitea.ISSUE_TYPE_LABELS["new-room"]
    seeded = httpx.post(
        f"{repo_api}/labels",
        headers=headers,
        json={"name": "new-room", **spec},
        timeout=10,
    )
    seeded.raise_for_status()
    canonical_id = seeded.json()["id"]

    issue = await gitea.create_gitea_issue(
        ctx=ctx,
        title="New room request: marketing",
        body="Requested by Phreddy Phlyntstone.",
        request_type="new-room",
    )

    verify = httpx.get(
        f"{repo_api}/issues/{issue.number}",
        headers=headers,
        timeout=10,
    )
    assert verify.status_code == 200
    assert [label["id"] for label in verify.json()["labels"]] == [canonical_id]
    # ... and no second 'new-room' label was manufactured behind it
    names = [
        label["name"]
        for label in gitea_admin.list_labels(
            gitea_server["host"],
            gitea_server["token"],
            gitea_server["owner"],
            gitea_server["repo"],
        )
    ]
    assert names.count("new-room") == 1
