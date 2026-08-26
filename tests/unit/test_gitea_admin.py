import argparse
import importlib.util
import pathlib
from unittest import mock

import pytest

from soliplex_concierge import gitea_admin

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
    """Patch the module's httpx.Client; return (patcher, sync client mock)."""
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
    return mock.patch.object(gitea_admin.httpx, "Client", ctor), client


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


def test_main_show(capsys):
    patched, _client = _patch_client(
        {
            "number": 7,
            "title": "New room: mkt",
            "state": "open",
            "user": {"login": "phreddy"},
            "html_url": f"{BASE}/7",
            "body": "Requested by Phreddy.",
        }
    )

    with patched:
        rc = gitea_admin.main(["show", "7", *_CONN])

    out = capsys.readouterr().out
    assert rc == 0
    assert "by: phreddy" in out
    assert "Requested by Phreddy." in out


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
    """Patch httpx.Client so get/post/patch yield a sequence of payloads.

    Every `httpx.Client()` context in the module reuses the one client mock,
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
    return mock.patch.object(gitea_admin.httpx, "Client", ctor), client


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
            params={"page": page, "limit": gitea_admin.LABEL_PAGE_SIZE},
        )
        for page in (1, 2, 3)
    ]


def test_list_labels_bounds_the_page_walk():
    # a server that ignored 'page' would otherwise be walked forever
    page = [{"name": "approved", "id": 1}]
    patched, client = _patch_client_seq(
        get=[page] * gitea_admin.MAX_LABEL_PAGES
    )

    with patched:
        result = gitea_admin.list_labels(HOST, TOKEN, OWNER, REPO)

    assert client.get.call_count == gitea_admin.MAX_LABEL_PAGES
    assert result == page * gitea_admin.MAX_LABEL_PAGES


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


def test_init_labels_all_missing():
    patched, client = _patch_client_seq(
        get=[[]], post=[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    )

    with patched:
        result = gitea_admin.init_labels(HOST, TOKEN, OWNER, REPO)

    assert result == {name: "created" for name in gitea_admin.LABELS}
    assert client.post.call_count == len(gitea_admin.LABELS)


def test_init_labels_partial():
    patched, client = _patch_client_seq(
        get=[[{"name": "approved", "id": 9}], []],
        post=[{"id": 1}, {"id": 2}, {"id": 3}],
    )

    with patched:
        result = gitea_admin.init_labels(HOST, TOKEN, OWNER, REPO)

    assert result["approved"] == "exists"
    assert result["new-room"] == "created"
    assert client.post.call_count == len(gitea_admin.LABELS) - 1


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
