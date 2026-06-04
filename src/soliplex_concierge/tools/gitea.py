"""Tools supporting the Gitea integration used by the 'about_soliplex' room.

Issue creation talks to the Gitea REST API directly (no MCP server): the
repository is fixed by the room's tool configuration, so the agent cannot
file against an arbitrary repository.
"""

import httpx
import pydantic
import pydantic_ai
from soliplex import agents

from soliplex_concierge import config


class CreatedGiteaIssue(pydantic.BaseModel):
    """A Gitea issue created on behalf of a room request."""

    number: int
    url: str
    title: str


async def create_gitea_issue(
    ctx: pydantic_ai.RunContext[agents.AgentDependencies],
    title: str,
    body: str,
) -> CreatedGiteaIssue:
    """File a room request as a Gitea issue and return its number and link.

    The target repository is fixed by this room's configuration, so you do
    not choose it -- just supply a clear, specific 'title' and a 'body' that
    captures the request type, the requesting user, and every detail you
    collected. Report the returned issue number and URL back to the user.

    Args:
        title: the issue title.
        body: the issue body (Markdown).

    Returns:
        CreatedGiteaIssue: the 'number', 'url', and 'title' of the new issue.
    """
    tool_config = ctx.deps.tool_configs[config.CGI_TOOL_KIND]
    installation = ctx.deps.the_installation

    host = installation.get_environment(tool_config.host_env_var)
    token = installation.get_secret(tool_config.token_secret_name)
    url = (
        f"{host.rstrip('/')}"
        f"/api/v1/repos/{tool_config.owner}/{tool_config.repo}/issues"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            headers={"Authorization": f"token {token}"},
            json={"title": title, "body": body},
        )
    response.raise_for_status()

    data = response.json()
    return CreatedGiteaIssue(
        number=data["number"],
        url=data["html_url"],
        title=data["title"],
    )
