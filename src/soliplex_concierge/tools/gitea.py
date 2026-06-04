"""Tools supporting the Gitea integration used by the 'about_soliplex' room."""

import pydantic
import pydantic_ai
from soliplex import agents

from soliplex_concierge import config


class GiteaIssueTarget(pydantic.BaseModel):
    """The Gitea repository where room requests should be filed."""

    owner: str
    repo: str


async def get_gitea_issue_target(
    ctx: pydantic_ai.RunContext[agents.AgentDependencies],
) -> GiteaIssueTarget:
    """Return the Gitea repository configured for filing room requests.

    Call this before creating a Gitea issue, then pass the returned 'owner'
    and 'repo' values verbatim as the new issue's 'owner' and 'repo'
    arguments. Never guess or invent repository coordinates.

    Returns:
        GiteaIssueTarget: the 'owner' and 'repo' of this room's configured
        issue-tracking repository.
    """
    tool_config = ctx.deps.tool_configs[config.GITC_TOOL_KIND]
    return GiteaIssueTarget(
        owner=tool_config.owner,
        repo=tool_config.repo,
    )
