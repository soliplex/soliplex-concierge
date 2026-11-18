import argparse
import importlib.util
import pathlib
from unittest import mock

import httpx2
import pytest

from soliplex_concierge import config
from soliplex_concierge import gitea_admin
from soliplex_concierge import labels as sc_labels
from soliplex_concierge.tools import gitea as sc_gitea

# The admin skill's 'gitea_issues.py' is now a thin shim over this module; its
# path is used by the shim smoke test at the bottom.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIM = (
    REPO_ROOT
    / "skills"
    / "soliplex-concierge-admin"
    / "scripts"
    / "gitea_issues.py"
)

HOST = "https://gitea.example.com"
TOKEN = "tok-abc123"
OWNER = "acme"
REPO = "widgets"
BASE = f"{HOST}/api/v1/repos/{OWNER}/{REPO}/issues"
HEADERS = {"Authorization": f"token {TOKEN}"}


def _patch_client(payload):
    """Patch the module's httpx2.Client; return (patcher, sync client mock)."""
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = payload

    client = mock.Mock()
    client.get.return_value = response
    client.post.return_value = response
    client.patch.return_value = response

    client_cm = mock.MagicMock()
    client_cm.__enter__.return_value = client
    client_cm.__exit__.return_value = False

    ctor = mock.Mock(return_value=client_cm)
    return mock.patch.object(gitea_admin.httpx2, "Client", ctor), client


def _routed(payload=None, text=None, error=None):
    response = mock.Mock()
    response.json.return_value = payload
    response.text = text
    response.raise_for_status = mock.Mock(side_effect=error)
    return response


def _patch_routes(routes):
    """Patch httpx2.Client so each GET answers from ``routes`` by URL.

    Returns (patcher, client mock, constructor mock).
    """
    client = mock.Mock()
    client.get.side_effect = lambda url, **kwargs: routes[url]

    client_cm = mock.MagicMock()
    client_cm.__enter__.return_value = client
    client_cm.__exit__.return_value = False

    ctor = mock.Mock(return_value=client_cm)
    return mock.patch.object(gitea_admin.httpx2, "Client", ctor), client, ctor


ASSET_URL = f"{HOST}/attachments/abc-uuid"
PROFILE_YAML = "preferred_username: phreddy\nemail: phreddy@example.com\n"
ISSUE = {
    "number": 7,
    "title": "Room access request: mkt",
    "state": "open",
    "user": {"login": "concierge"},
    "html_url": f"{BASE}/7",
    "labels": [{"name": "room-access"}],
    "body": "- **Requested by:** Somebody Else",
}


def _asset(name=gitea_admin.DEFAULT_PROFILE_NAME):
    return {"name": name, "browser_download_url": ASSET_URL}


def _fallback_comment(name=gitea_admin.DEFAULT_PROFILE_NAME):
    """The comment the room tool posts when Gitea refuses the attachment."""
    steps = sc_gitea._record_user_profile(
        f"{HOST}/api/v1/repos/{OWNER}/{REPO}",
        HEADERS,
        7,
        {"preferred_username": "phreddy", "email": "phreddy@example.com"},
        name,
        [],
    )
    next(steps)  # the attachment upload
    _method, _url, kwargs = steps.throw(httpx2.TransportError("refused"))
    return kwargs["json"]["body"]


def _comment(body, login="concierge"):
    return {"user": {"login": login}, "body": body, "html_url": f"{BASE}/7#c1"}


def _routes(assets=(), comments=(), download=None):
    return {
        f"{BASE}/7": _routed(ISSUE),
        f"{BASE}/7/assets": _routed(list(assets)),
        f"{BASE}/7/comments": _routed(list(comments)),
        ASSET_URL: download or _routed(text=PROFILE_YAML),
    }


# --- HTTP helpers ---------------------------------------------------------


def test_list_issues():
    patched, client = _patch_client([{"number": 7}])

    with patched:
        result = gitea_admin.list_issues(HOST, TOKEN, OWNER, REPO)

    assert result == [{"number": 7}]
    client.get.assert_called_once_with(
        BASE,
        headers=HEADERS,
        params={"state": "open", "type": "issues"},
    )


def test_list_issues_with_labels():
    patched, client = _patch_client([])

    with patched:
        gitea_admin.list_issues(
            HOST,
            TOKEN,
            OWNER,
            REPO,
            state="all",
            labels=["room-access", "other"],
        )

    client.get.assert_called_once_with(
        BASE,
        headers=HEADERS,
        params={
            "state": "all",
            "type": "issues",
            "labels": "room-access,other",
        },
    )


def test_get_issue():
    patched, client = _patch_client({"number": 7, "title": "t"})

    with patched:
        result = gitea_admin.get_issue(HOST, TOKEN, OWNER, REPO, 7)

    assert result == {"number": 7, "title": "t"}
    client.get.assert_called_once_with(f"{BASE}/7", headers=HEADERS)


def test_list_issues_created_by():
    patched, client = _patch_client([])

    with patched:
        gitea_admin.list_issues(
            HOST, TOKEN, OWNER, REPO, created_by="concierge"
        )

    client.get.assert_called_once_with(
        BASE,
        headers=HEADERS,
        params={"state": "open", "type": "issues", "created_by": "concierge"},
    )


@pytest.mark.parametrize(
    "helper, suffix",
    [
        (gitea_admin.list_issue_assets, "assets"),
        (gitea_admin.list_comments, "comments"),
    ],
)
def test_issue_subresources(helper, suffix):
    patched, client = _patch_client([{"id": 1}])

    with patched:
        result = helper(HOST, TOKEN, OWNER, REPO, 7)

    assert result == [{"id": 1}]
    client.get.assert_called_once_with(f"{BASE}/7/{suffix}", headers=HEADERS)


def test_download_asset():
    patched, client, ctor = _patch_routes({ASSET_URL: _routed(text="x")})

    with patched:
        result = gitea_admin.download_asset(TOKEN, ASSET_URL)

    assert result == "x"
    assert ctor.call_args.kwargs["follow_redirects"] is True
    client.get.assert_called_once_with(ASSET_URL, headers=HEADERS)


def test_default_profile_name_matches_the_room_tool():
    result = gitea_admin.DEFAULT_PROFILE_NAME

    assert result == config.DEFAULT_PROFILE_ATTACHMENT_NAME


@pytest.mark.parametrize(
    "routes, expected",
    [
        (
            _routes(assets=[_asset("other.txt"), _asset()]),
            gitea_admin.Profile("attachment", PROFILE_YAML, ASSET_URL),
        ),
        (
            _routes(
                assets=[_asset()],
                download=_routed(error=httpx2.TransportError("no")),
            ),
            gitea_admin.Profile("attachment", None, ASSET_URL),
        ),
        (
            _routes(
                comments=[_comment("hello"), _comment(_fallback_comment())]
            ),
            gitea_admin.Profile("comment", PROFILE_YAML, f"{BASE}/7#c1"),
        ),
        (_routes(comments=[_comment(_fallback_comment(), "mallory")]), None),
        (_routes(comments=[_comment(_fallback_comment("other.yaml"))]), None),
        (_routes(), None),
    ],
    ids=[
        "attachment",
        "attachment-undownloadable",
        "filer-comment",
        "other-account-comment",
        "other-name-comment",
        "none",
    ],
)
def test_get_profile(routes, expected):
    patched, _client, _ctor = _patch_routes(routes)

    with patched:
        result = gitea_admin.get_profile(HOST, TOKEN, OWNER, REPO, ISSUE)

    assert result == expected


def test_comment_issue():
    patched, client = _patch_client({"html_url": f"{BASE}/7#issuecomment-1"})

    with patched:
        result = gitea_admin.comment_issue(HOST, TOKEN, OWNER, REPO, 7, "done")

    assert result == {"html_url": f"{BASE}/7#issuecomment-1"}
    client.post.assert_called_once_with(
        f"{BASE}/7/comments",
        headers=HEADERS,
        json={"body": "done"},
    )


def test_close_issue_without_comment():
    patched, client = _patch_client({"number": 7, "state": "closed"})

    with patched:
        result = gitea_admin.close_issue(HOST, TOKEN, OWNER, REPO, 7)

    assert result == {"number": 7, "state": "closed"}
    client.post.assert_not_called()
    client.patch.assert_called_once_with(
        f"{BASE}/7",
        headers=HEADERS,
        json={"state": "closed"},
    )


def test_close_issue_with_comment():
    patched, client = _patch_client({"number": 7, "state": "closed"})

    with patched:
        gitea_admin.close_issue(HOST, TOKEN, OWNER, REPO, 7, body="bye")

    client.post.assert_called_once_with(
        f"{BASE}/7/comments",
        headers=HEADERS,
        json={"body": "bye"},
    )
    client.patch.assert_called_once_with(
        f"{BASE}/7",
        headers=HEADERS,
        json={"state": "closed"},
    )


# --- connection resolution ------------------------------------------------


def test_resolve_conn_from_env(monkeypatch):
    monkeypatch.setenv("GITEA_HOST", HOST)
    monkeypatch.setenv("GITEA_ACCESS_TOKEN", TOKEN)
    args = argparse.Namespace(host=None, token=None)

    result = gitea_admin._resolve_conn(args)

    assert result == (HOST, TOKEN)


def test_resolve_conn_missing_raises(monkeypatch):
    monkeypatch.delenv("GITEA_HOST", raising=False)
    monkeypatch.delenv("GITEA_ACCESS_TOKEN", raising=False)
    args = argparse.Namespace(host=None, token=None)

    with pytest.raises(SystemExit, match="GITEA_HOST"):
        gitea_admin._resolve_conn(args)


# --- main dispatch --------------------------------------------------------

_CONN = ["--host", HOST, "--token", TOKEN, "--owner", OWNER, "--repo", REPO]


def test_main_list(capsys):
    patched, _client = _patch_client(
        [{"number": 7, "title": "New room: mkt", "html_url": f"{BASE}/7"}]
    )

    with patched:
        rc = gitea_admin.main(["list", *_CONN])

    assert rc == 0
    assert "#7\tNew room: mkt" in capsys.readouterr().out


def test_main_list_empty(capsys):
    patched, _client = _patch_client([])

    with patched:
        rc = gitea_admin.main(["list", *_CONN])

    assert rc == 0
    assert "no open issues" in capsys.readouterr().out


@pytest.mark.parametrize(
    "routes, expected",
    [
        (
            _routes(assets=[_asset()]),
            [
                "--- requester's profile (from their token; attachment "
                f"{ASSET_URL}) ---",
                "preferred_username: phreddy",
            ],
        ),
        (
            _routes(comments=[_comment(_fallback_comment())]),
            [
                "--- requester's profile (from their token; comment "
                f"{BASE}/7#c1) ---",
                "email: phreddy@example.com",
            ],
        ),
        (
            _routes(
                assets=[_asset()],
                download=_routed(error=httpx2.TransportError("no")),
            ),
            ["could not be downloaded; open " + ASSET_URL],
        ),
        (_routes(), ["--- requester's profile: none recorded on this issue"]),
    ],
    ids=["attachment", "comment", "undownloadable", "none"],
)
def test_main_show(capsys, routes, expected):
    patched, _client, _ctor = _patch_routes(routes)

    with patched:
        rc = gitea_admin.main(["show", "7", *_CONN])

    out = capsys.readouterr().out
    assert rc == 0
    assert "by: concierge" in out
    assert "labels: room-access" in out
    for line in expected:
        assert line in out
    assert out.index("requester's profile") < out.index("--- body")
    assert out.rstrip().endswith("- **Requested by:** Somebody Else")


def test_main_show_without_labels_or_body(capsys):
    routes = _routes()
    routes[f"{BASE}/7"] = _routed({**ISSUE, "labels": [], "body": ""})
    patched, _client, _ctor = _patch_routes(routes)

    with patched:
        rc = gitea_admin.main(["show", "7", *_CONN])

    out = capsys.readouterr().out
    assert rc == 0
    assert "labels: (none)" in out
    assert out.rstrip().endswith("(no body)")


def test_main_show_with_a_profile_name(capsys):
    patched, _client, _ctor = _patch_routes(
        _routes(assets=[_asset("profile.yml")])
    )

    with patched:
        rc = gitea_admin.main(
            ["show", "7", "--profile-name", "profile.yml", *_CONN]
        )

    assert rc == 0
    assert "preferred_username: phreddy" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command", [["list"], ["search", "--type", "new-room"]]
)
def test_main_created_by(command):
    patched, client = _patch_client([])

    with patched:
        gitea_admin.main([*command, "--created-by", "concierge", *_CONN])

    assert client.get.call_args.kwargs["params"]["created_by"] == "concierge"


def test_main_comment(capsys):
    patched, _client = _patch_client({"html_url": f"{BASE}/7#c1"})

    with patched:
        rc = gitea_admin.main(["comment", "7", "--body", "ok", *_CONN])

    assert rc == 0
    assert "commented on #7" in capsys.readouterr().out


def test_main_close(capsys):
    patched, _client = _patch_client({"number": 7, "state": "closed"})

    with patched:
        rc = gitea_admin.main(["close", "7", *_CONN])

    assert rc == 0
    assert "closed #7" in capsys.readouterr().out


# --- labels ---------------------------------------------------------------

LABELS_URL = f"{HOST}/api/v1/repos/{OWNER}/{REPO}/labels"


def _response(payload):
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = payload
    return response


def _patch_client_seq(get=(), post=(), patch=()):
    """Patch httpx2.Client so get/post/patch yield a sequence of payloads.

    Every `httpx2.Client()` context in the module reuses the one client mock,
    so the side_effect lists are consumed in call order across helpers.
    """
    client = mock.Mock()
    client.get.side_effect = [_response(p) for p in get]
    client.post.side_effect = [_response(p) for p in post]
    client.patch.side_effect = [_response(p) for p in patch]

    client_cm = mock.MagicMock()
    client_cm.__enter__.return_value = client
    client_cm.__exit__.return_value = False

    ctor = mock.Mock(return_value=client_cm)
    return mock.patch.object(gitea_admin.httpx2, "Client", ctor), client


def test_list_labels():
    patched, client = _patch_client_seq(
        get=[
            [{"name": "approved", "id": 1}],
            [{"name": "denied", "id": 2}],
            [],
        ]
    )

    with patched:
        result = gitea_admin.list_labels(HOST, TOKEN, OWNER, REPO)

    # every page is walked, so a label past the first is not reported missing
    assert result == [
        {"name": "approved", "id": 1},
        {"name": "denied", "id": 2},
    ]
    assert client.get.call_args_list == [
        mock.call(
            LABELS_URL,
            headers=HEADERS,
            params={"page": page, "limit": sc_labels.LABEL_PAGE_SIZE},
        )
        for page in (1, 2, 3)
    ]


def test_list_labels_bounds_the_page_walk():
    # a server that ignored 'page' would otherwise be walked forever
    page = [{"name": "approved", "id": 1}]
    patched, client = _patch_client_seq(get=[page] * sc_labels.MAX_LABEL_PAGES)

    with patched:
        result = gitea_admin.list_labels(HOST, TOKEN, OWNER, REPO)

    assert client.get.call_count == sc_labels.MAX_LABEL_PAGES
    assert result == page * sc_labels.MAX_LABEL_PAGES


def test_create_label():
    patched, client = _patch_client({"id": 5, "name": "approved"})

    with patched:
        result = gitea_admin.create_label(
            HOST, TOKEN, OWNER, REPO, "approved", "#0e8a16", "approved!"
        )

    assert result == {"id": 5, "name": "approved"}
    client.post.assert_called_once_with(
        LABELS_URL,
        headers=HEADERS,
        json={
            "name": "approved",
            "color": "#0e8a16",
            "description": "approved!",
        },
    )


def test_add_labels_to_issue():
    patched, client = _patch_client([{"name": "approved", "id": 5}])

    with patched:
        result = gitea_admin.add_labels_to_issue(
            HOST, TOKEN, OWNER, REPO, 7, [5]
        )

    assert result == [{"name": "approved", "id": 5}]
    client.post.assert_called_once_with(
        f"{BASE}/7/labels",
        headers=HEADERS,
        json={"labels": [5]},
    )


def test_resolve_label_ids():
    patched, _client = _patch_client_seq(
        get=[[{"name": "approved", "id": 5}, {"name": "denied", "id": 6}], []]
    )

    with patched:
        result = gitea_admin._resolve_label_ids(
            HOST, TOKEN, OWNER, REPO, ["denied", "approved"]
        )

    assert result == [6, 5]


def test_resolve_label_ids_missing_raises():
    patched, _client = _patch_client_seq(
        get=[[{"name": "approved", "id": 5}], []]
    )

    with patched, pytest.raises(ValueError, match="init"):
        gitea_admin._resolve_label_ids(HOST, TOKEN, OWNER, REPO, ["denied"])


def test_label_issue():
    patched, client = _patch_client_seq(
        get=[[{"name": "denied", "id": 8}], []],
        post=[[{"name": "denied", "id": 8}]],
    )

    with patched:
        result = gitea_admin.label_issue(
            HOST, TOKEN, OWNER, REPO, 7, ["denied"]
        )

    assert result == [{"name": "denied", "id": 8}]
    client.post.assert_called_once_with(
        f"{BASE}/7/labels",
        headers=HEADERS,
        json={"labels": [8]},
    )


def test_label_issue_missing_raises():
    patched, _client = _patch_client_seq(
        get=[[{"name": "approved", "id": 7}], []]
    )

    with patched, pytest.raises(ValueError, match="provision-failed"):
        gitea_admin.label_issue(
            HOST, TOKEN, OWNER, REPO, 7, ["provision-failed"]
        )


def test_init_labels_all_missing():
    patched, client = _patch_client_seq(
        get=[[]], post=[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    )

    with patched:
        result = gitea_admin.init_labels(HOST, TOKEN, OWNER, REPO)

    assert result == {name: "created" for name in sc_labels.REQUEST_LABELS}
    assert client.post.call_count == len(sc_labels.REQUEST_LABELS)


def test_init_labels_partial():
    patched, client = _patch_client_seq(
        get=[[{"name": "approved", "id": 9}], []],
        post=[{"id": 1}, {"id": 2}, {"id": 3}],
    )

    with patched:
        result = gitea_admin.init_labels(HOST, TOKEN, OWNER, REPO)

    assert result["approved"] == "exists"
    assert result["new-room"] == "created"
    assert client.post.call_count == len(sc_labels.REQUEST_LABELS) - 1


# --- decision helpers -----------------------------------------------------


def test_approve_issue_with_body():
    patched, client = _patch_client_seq(
        get=[[{"name": "approved", "id": 5}], []],
        post=[[{"name": "approved"}], {"html_url": "c"}],
        patch=[{"number": 7, "state": "closed"}],
    )

    with patched:
        result = gitea_admin.approve_issue(
            HOST, TOKEN, OWNER, REPO, 7, body="granted"
        )

    assert result == {"number": 7, "state": "closed"}
    add_call, comment_call = client.post.call_args_list
    assert add_call == mock.call(
        f"{BASE}/7/labels", headers=HEADERS, json={"labels": [5]}
    )
    assert comment_call == mock.call(
        f"{BASE}/7/comments",
        headers=HEADERS,
        json={"body": "**Decision: APPROVED**\n\ngranted"},
    )
    client.patch.assert_called_once_with(
        f"{BASE}/7", headers=HEADERS, json={"state": "closed"}
    )


def test_deny_issue_without_body():
    patched, client = _patch_client_seq(
        get=[[{"name": "denied", "id": 6}], []],
        post=[[{"name": "denied"}], {"html_url": "c"}],
        patch=[{"number": 8, "state": "closed"}],
    )

    with patched:
        result = gitea_admin.deny_issue(HOST, TOKEN, OWNER, REPO, 8)

    assert result == {"number": 8, "state": "closed"}
    _add_call, comment_call = client.post.call_args_list
    assert comment_call == mock.call(
        f"{BASE}/8/comments",
        headers=HEADERS,
        json={"body": "**Decision: DENIED**"},
    )


# --- main dispatch for the new subcommands --------------------------------


def test_main_init(capsys):
    patched, _client = _patch_client_seq(
        get=[[]], post=[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    )

    with patched:
        rc = gitea_admin.main(["init", *_CONN])

    assert rc == 0
    assert "new-room: created" in capsys.readouterr().out


def test_main_search(capsys):
    patched, client = _patch_client(
        [
            {
                "number": 7,
                "title": "Room access request: chat",
                "html_url": f"{BASE}/7",
            }
        ]
    )

    with patched:
        rc = gitea_admin.main(
            [
                "search",
                "--type",
                "room-access",
                "--decision",
                "approved",
                "--label",
                "x",
                *_CONN,
            ]
        )

    assert rc == 0
    assert "#7\tRoom access request: chat" in capsys.readouterr().out
    client.get.assert_called_once_with(
        BASE,
        headers=HEADERS,
        params={
            "state": "open",
            "type": "issues",
            "labels": "x,room-access,approved",
        },
    )


def test_main_search_empty(capsys):
    patched, _client = _patch_client([])

    with patched:
        rc = gitea_admin.main(["search", *_CONN])

    assert rc == 0
    assert "no matching open issues" in capsys.readouterr().out


def test_main_approve(capsys):
    patched, _client = _patch_client_seq(
        get=[[{"name": "approved", "id": 5}], []],
        post=[[{"name": "approved"}], {"html_url": "c"}],
        patch=[{"number": 7, "state": "closed"}],
    )

    with patched:
        rc = gitea_admin.main(["approve", "7", "--body", "ok", *_CONN])

    assert rc == 0
    assert "approved #7" in capsys.readouterr().out


def test_main_deny(capsys):
    patched, _client = _patch_client_seq(
        get=[[{"name": "denied", "id": 6}], []],
        post=[[{"name": "denied"}], {"html_url": "c"}],
        patch=[{"number": 8, "state": "closed"}],
    )

    with patched:
        rc = gitea_admin.main(["deny", "8", *_CONN])

    assert rc == 0
    assert "denied #8" in capsys.readouterr().out


# --- the admin skill's shim -----------------------------------------------


def test_admin_shim_delegates_to_library():
    spec = importlib.util.spec_from_file_location("gitea_issues_shim", SHIM)
    shim = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(shim)

    assert shim.main is gitea_admin.main
