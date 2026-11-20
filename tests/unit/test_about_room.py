"""The shipped about-room, run as a real agent under a scripted model.

The installation is built from the room's own 'room_config.yaml' and
'prompt.txt', and the tools run their real code against faked HTTP services.
"""

import json
import pathlib
import re
import shutil
from unittest import mock

import httpx2
import pytest
import yaml
from pydantic_ai import messages as ai_messages
from pydantic_ai.models import function as ai_function
from soliplex import installation
from soliplex import models
from soliplex.config import installation as config_installation

from soliplex_concierge.tools import gitea
from soliplex_concierge.tools import provisioner

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
SNIPPET = ROOM_DIR.parents[1] / "installation-snippet.yaml"
PROMPT = ROOM_DIR / "prompt.txt"

REPO_PATH = "/api/v1/repos/your-gitea-owner/soliplex-requests"
ISSUE_URL = (
    f"http://gitea.test{REPO_PATH.removeprefix('/api/v1/repos')}/issues/7"
)

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


def _services(routes, sent=None):
    """Patch 'httpx2.AsyncClient' to answer each request from 'routes'.

    'routes' maps '(method, path)' to the 'httpx2.Response' answering it, or
    to the exception the request raises. Each request is appended to 'sent'.
    """

    def answer(request):
        if sent is not None:
            sent.append(request)
        answer = routes[(request.method, request.url.path)]
        if isinstance(answer, Exception):
            raise answer
        return answer

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


async def _run(the_installation, model, user=USER):
    agent = await the_installation.get_agent_for_room(
        room_id=ROOM_ID, user=USER.model_dump()
    )
    deps = await the_installation.get_agent_deps_for_room(
        room_id=ROOM_ID, user=user
    )
    with agent.override(model=model):
        return await agent.run("I need a room for my Q3 plans", deps=deps)


def _tool_returns(result) -> dict[str, ai_messages.ToolReturnPart]:
    """What each tool returned to the model during the run, by tool name."""
    return {
        part.tool_name: part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ai_messages.ToolReturnPart)
    }


@pytest.mark.anyio
async def test_request_room_accepted(the_installation):
    routes = {
        ("POST", "/rooms"): httpx2.Response(
            202,
            json={"room_id": "phreddy--q3-plans", "issue_url": ISSUE_URL},
        ),
    }
    model = _scripted(
        ("request_room", {"name": "Q3 Plans", "description": "Q3 planning"})
    )

    with _services(routes):
        result = await _run(the_installation, model)

    returned = _tool_returns(result)["request_room"].content
    assert isinstance(returned, provisioner.RoomRequestAccepted)
    assert returned.status == "accepted"
    assert returned.room_id == "phreddy--q3-plans"
    assert returned.issue_url == ISSUE_URL
    assert result.output == "done"


# ---- filing an issue ----

NEW_ROOM = {"kind": "new-room", "name": "Q3 Plans", "purpose": "Q3 planning"}
TITLE = "New room request: Q3 Plans"
BODY = """\
# New room request: Q3 Plans

- **Request type:** New room

## Purpose

Q3 planning

## Knowledge sources

*(not provided)*

## Access

*(not provided)*

## Tools & skills

*(not provided)*

## Additional notes

*(not provided)*
"""
ROOM_ACCESS = {
    "kind": "room-access",
    "room": "finance",
    "justification": "I review the budget",
    "notes": "Read access is enough",
}
ACCESS_TITLE = "Room access request: finance"
ACCESS_BODY = """\
# Room access request: finance

- **Request type:** Access to an existing private room

## Room

finance

## Justification

I review the budget

## Additional notes

Read access is enough
"""
LABELS = ("GET", f"{REPO_PATH}/labels")
ISSUES = ("POST", f"{REPO_PATH}/issues")
LABEL_LISTING = httpx2.Response(200, json=[{"name": "new-room", "id": 5}])


def _filed(labels=("new-room",)):
    return httpx2.Response(
        201,
        json={
            "number": 7,
            "html_url": ISSUE_URL,
            "title": TITLE,
            "labels": [{"name": name} for name in labels],
        },
    )


async def _file(
    the_installation, routes, user=USER, request=NEW_ROOM, sent=None
):
    model = _scripted(("create_gitea_issue", {"request": request}))
    with _services(routes, sent):
        result = await _run(the_installation, model, user)
    assert result.output == "done"
    return _tool_returns(result)["create_gitea_issue"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_, label, title, body",
    [
        (NEW_ROOM, "new-room", TITLE, BODY),
        (ROOM_ACCESS, "room-access", ACCESS_TITLE, ACCESS_BODY),
    ],
    ids=["new-room", "room-access"],
)
async def test_file_issue(the_installation, request_, label, title, body):
    sent = []
    returned = await _file(
        the_installation,
        {
            LABELS: httpx2.Response(200, json=[{"name": label, "id": 5}]),
            ISSUES: _filed(labels=(label,)),
            ("POST", f"{REPO_PATH}/issues/7/assets"): httpx2.Response(201),
        },
        request=request_,
        sent=sent,
    )

    assert returned.outcome == "success"
    issue_post = next(r for r in sent if r.url.path == ISSUES[1])
    assert json.loads(issue_post.content) == {
        "title": title,
        "body": body,
        "labels": [5],
    }


@pytest.mark.anyio
async def test_file_issue_with_a_missing_profile(the_installation):
    returned = await _file(
        the_installation,
        {
            LABELS: LABEL_LISTING,
            ISSUES: _filed(),
            ("POST", f"{REPO_PATH}/issues/7/assets"): httpx2.Response(415),
            ("POST", f"{REPO_PATH}/issues/7/comments"): httpx2.Response(500),
        },
    )

    assert returned.outcome == "success"
    assert isinstance(returned.content, gitea.CreatedGiteaIssue)
    assert returned.content.number == 7
    assert returned.content.profile == "missing"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "routes, user",
    [
        ({LABELS: LABEL_LISTING, ISSUES: httpx2.Response(500)}, USER),
        ({LABELS: httpx2.Response(401)}, USER),
        ({}, None),
    ],
    ids=["issue-refused", "labels-refused", "anonymous"],
)
async def test_file_issue_not_filed(the_installation, routes, user):
    returned = await _file(the_installation, routes, user)

    assert returned.outcome == "failed"
    assert TITLE in returned.content
    assert BODY in returned.content
    assert "submit" in returned.content


@pytest.mark.anyio
async def test_file_issue_filed_without_its_label(the_installation):
    returned = await _file(
        the_installation, {LABELS: LABEL_LISTING, ISSUES: _filed(labels=())}
    )

    assert returned.outcome == "failed"
    assert ISSUE_URL in returned.content
    assert "do not file" in returned.content
    assert BODY not in returned.content


@pytest.mark.anyio
async def test_file_issue_maybe_filed(the_installation):
    returned = await _file(
        the_installation,
        {LABELS: LABEL_LISTING, ISSUES: httpx2.ReadTimeout("no answer")},
    )

    assert returned.outcome == "failed"
    assert "may exist" in returned.content
    assert "do not file" in returned.content
    assert BODY not in returned.content


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(201, text="<html>"),
        httpx2.Response(201, json=None),
        httpx2.Response(201, json=[]),
        httpx2.Response(201, json={"number": 7}),
    ],
    ids=["not-json", "null", "list", "missing-fields"],
)
async def test_file_issue_maybe_filed_after_a_malformed_answer(
    the_installation, answer
):
    returned = await _file(
        the_installation, {LABELS: LABEL_LISTING, ISSUES: answer}
    )

    assert returned.outcome == "failed"
    assert "may exist" in returned.content
    assert BODY not in returned.content


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_",
    [
        {"kind": "bogus", "name": "Q3 Plans", "purpose": "Q3 planning"},
        {"kind": "new-room", "name": "", "purpose": "Q3 planning"},
        {"kind": "room-access", "room": "finance"},
    ],
    ids=["unknown-kind", "empty-name", "missing-justification"],
)
async def test_file_issue_rejects_an_invalid_request(
    the_installation, request_
):
    sent = []
    model = _scripted(("create_gitea_issue", {"request": request_}))

    with _services({}, sent):
        result = await _run(the_installation, model)

    retries = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ai_messages.RetryPromptPart)
    ]
    assert [part.tool_name for part in retries] == ["create_gitea_issue"]
    assert sent == []
    assert result.output == "done"


@pytest.mark.anyio
async def test_not_admitted_then_file_issue(the_installation):
    sent = []
    routes = {
        ("POST", "/rooms"): httpx2.Response(403, json={"detail": "no"}),
        LABELS: LABEL_LISTING,
        ISSUES: _filed(),
        ("POST", f"{REPO_PATH}/issues/7/assets"): httpx2.Response(201),
    }
    model = _scripted(
        ("request_room", {"name": "Q3 Plans", "description": "Q3 planning"}),
        ("create_gitea_issue", {"request": NEW_ROOM}),
    )

    with _services(routes, sent):
        result = await _run(the_installation, model)

    returned = _tool_returns(result)
    refusal = returned["request_room"].content
    assert isinstance(refusal, provisioner.RoomRequestAccepted)
    assert refusal.status == "not_admitted"
    assert returned["create_gitea_issue"].outcome == "success"
    issue_post = next(r for r in sent if r.url.path == ISSUES[1])
    assert json.loads(issue_post.content)["body"] == BODY
    assert result.output == "done"


def _property_names(schema) -> set[str]:
    """Every property name anywhere in a JSON schema."""
    if isinstance(schema, list):
        return set().union(*(_property_names(item) for item in schema))
    if not isinstance(schema, dict):
        return set()
    names = set(schema.get("properties", {}))
    return names.union(*(_property_names(value) for value in schema.values()))


@pytest.mark.anyio
async def test_prompt_names_only_the_room_tools(the_installation):
    """Each quoted 'snake_case' word in the prompt names a tool or argument."""
    named = set(re.findall(r"'([a-z]+(?:_[a-z]+)+)'", PROMPT.read_text()))
    tools, arguments = set(), set()

    def respond(_messages, info):
        for tool in info.function_tools:
            tools.add(tool.name)
            arguments.update(_property_names(tool.parameters_json_schema))
        return ai_messages.ModelResponse(parts=[ai_messages.TextPart("done")])

    await _run(the_installation, ai_function.FunctionModel(respond))

    assert {"request_room", "create_gitea_issue"} <= named & tools
    assert named <= tools | arguments
