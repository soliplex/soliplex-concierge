"""The shipped about-room, run as a real agent under a scripted model.

The installation is built from the room's own 'room_config.yaml' and
'prompt.txt', and the tools run their real code against faked HTTP services.
"""

import pathlib
import shutil
import typing
from unittest import mock

import httpx2
import pytest
import yaml
from pydantic_ai import messages as ai_messages
from pydantic_ai.models import function as ai_function
from soliplex import installation
from soliplex import models
from soliplex.config import installation as config_installation

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROOM_ID = "about_soliplex"
ROOM_DIR = (
    REPO_ROOT
    / "skills"
    / "soliplex-concierge-installer"
    / "assets"
    / "rooms"
    / ROOM_ID
)
ROOM_SKILL = REPO_ROOT / "skills" / "soliplex-concierge-room"
SNIPPET = ROOM_DIR.parents[1] / "installation-snippet.yaml"

PROVISIONER_URL = "http://soliplex-provisioner:8090"
ISSUE_URL = "http://gitea.test/your-gitea-owner/soliplex-requests/issues/7"

# The tool-config registrations come from the shipped snippet.
INSTALLATION_YAML = """\
id: "about-room-test"
secrets:
  - secret_name: "URL_SAFE_TOKEN_SECRET"
    sources: [{kind: "random_chars"}]
  - secret_name: "GITEA_ACCESS_TOKEN"
    sources: [{kind: "random_chars"}]
environment:
  - name: "GITEA_HOST"
    value: "http://gitea.test"
  - name: "OLLAMA_BASE_URL"
    value: "http://ollama.test"
agent_configs:
  - id: "default_chat"
    model_name: "scripted"
    system_prompt: "unused: the room has its own prompt"
filesystem_skills_paths:
  - "./skills"
room_paths:
  - "./rooms"
"""

DOCS_SKILL = """\
---
name: soliplex-docs
description: The Soliplex documentation.
---
Stand-in for the published 'soliplex-docs' skill.
"""

USER = models.UserProfile.from_user_claims(
    {
        "preferred_username": "phreddy",
        "email": "phreddy@example.com",
        "given_name": "Phreddy",
        "family_name": "Phlyntstone",
    }
)


@pytest.fixture
def the_installation(temp_dir) -> installation.Installation:
    shutil.copytree(ROOM_DIR, temp_dir / "rooms" / ROOM_ID)
    shutil.copytree(ROOM_SKILL, temp_dir / "skills" / ROOM_SKILL.name)
    docs = temp_dir / "skills" / "soliplex-docs"
    docs.mkdir(parents=True)
    (docs / "SKILL.md").write_text(DOCS_SKILL)
    config_dict = yaml.safe_load(INSTALLATION_YAML)
    config_dict["meta"] = yaml.safe_load(SNIPPET.read_text())["meta"]
    config_path = temp_dir / "installation.yaml"
    config_path.write_text(yaml.safe_dump(config_dict))

    the_installation = installation.Installation(
        config_installation.load_installation(config_path)
    )
    the_installation.resolve_secrets()
    the_installation.resolve_environment()
    return the_installation


def _services(routes):
    """Patch 'httpx2.AsyncClient' to answer each request from 'routes'.

    'routes' maps '(method, url)' to the 'httpx2.Response' answering it.
    """

    def answer(request):
        return routes[(request.method, str(request.url))]

    # The model's OpenAI client checks 'isinstance(.., httpx2.AsyncClient)'.
    class Client(httpx2.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx2.MockTransport(answer), **kwargs)

    return mock.patch.object(httpx2, "AsyncClient", Client)


def _scripted(*tool_calls):
    """A model making each of 'tool_calls' in turn, then answering "done".

    Each tool call is a '(tool name, arguments)' pair.
    """
    pending = list(tool_calls)

    def respond(_messages, _info):
        if pending:
            name, args = pending.pop(0)
            return ai_messages.ModelResponse(
                parts=[ai_messages.ToolCallPart(name, args)]
            )
        return ai_messages.ModelResponse(parts=[ai_messages.TextPart("done")])

    return ai_function.FunctionModel(respond)


async def _run(the_installation, model):
    agent = await the_installation.get_agent_for_room(
        room_id=ROOM_ID, user=USER.model_dump()
    )
    deps = await the_installation.get_agent_deps_for_room(
        room_id=ROOM_ID, user=USER
    )
    with agent.override(model=model):
        return await agent.run("I need a room for my Q3 plans", deps=deps)


def _tool_returns(result) -> dict[str, typing.Any]:
    """What each tool returned to the model during the run, by tool name."""
    return {
        part.tool_name: part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ai_messages.ToolReturnPart)
    }


@pytest.mark.anyio
async def test_request_room_accepted(the_installation):
    routes = {
        ("POST", f"{PROVISIONER_URL}/rooms"): httpx2.Response(
            202,
            json={"room_id": "phreddy--q3-plans", "issue_url": ISSUE_URL},
        ),
    }
    model = _scripted(
        ("request_room", {"name": "Q3 Plans", "description": "Q3 planning"})
    )

    with _services(routes):
        result = await _run(the_installation, model)

    returned = _tool_returns(result)["request_room"]
    assert returned.status == "accepted"
    assert returned.room_id == "phreddy--q3-plans"
    assert returned.issue_url == ISSUE_URL
    assert result.output == "done"
