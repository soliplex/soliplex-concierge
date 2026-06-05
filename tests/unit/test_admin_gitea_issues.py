import argparse
import importlib.util
import pathlib
from unittest import mock

import pytest

from soliplex_concierge import installer

# The admin skill's script lives outside the package, under the repo's
# 'skills/' tree; load it by path so we can unit-test its HTTP logic.
REPO_ROOT = pathlib.Path(installer.__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "skills"
    / "soliplex-concierge-admin"
    / "scripts"
    / "gitea_issues.py"
)
_spec = importlib.util.spec_from_file_location("gitea_issues", SCRIPT)
gitea_issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gitea_issues)

HOST = "https://gitea.example.com"
TOKEN = "tok-abc123"
OWNER = "acme"
REPO = "widgets"
BASE = f"{HOST}/api/v1/repos/{OWNER}/{REPO}/issues"
HEADERS = {"Authorization": f"token {TOKEN}"}


def _patch_client(payload):
    """Patch the script's httpx.Client; return (patcher, sync client mock)."""
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
    return mock.patch.object(gitea_issues.httpx, "Client", ctor), client


# --- HTTP helpers ---------------------------------------------------------


def test_list_issues():
    patched, client = _patch_client([{"number": 7}])

    with patched:
        result = gitea_issues.list_issues(HOST, TOKEN, OWNER, REPO)

    assert result == [{"number": 7}]
    client.get.assert_called_once_with(
        BASE,
        headers=HEADERS,
        params={"state": "open", "type": "issues"},
    )


def test_list_issues_with_labels():
    patched, client = _patch_client([])

    with patched:
        gitea_issues.list_issues(
            HOST, TOKEN, OWNER, REPO, state="all", labels=["room", "access"]
        )

    client.get.assert_called_once_with(
        BASE,
        headers=HEADERS,
        params={"state": "all", "type": "issues", "labels": "room,access"},
    )


def test_get_issue():
    patched, client = _patch_client({"number": 7, "title": "t"})

    with patched:
        result = gitea_issues.get_issue(HOST, TOKEN, OWNER, REPO, 7)

    assert result == {"number": 7, "title": "t"}
    client.get.assert_called_once_with(f"{BASE}/7", headers=HEADERS)


def test_comment_issue():
    patched, client = _patch_client({"html_url": f"{BASE}/7#issuecomment-1"})

    with patched:
        result = gitea_issues.comment_issue(
            HOST, TOKEN, OWNER, REPO, 7, "done"
        )

    assert result == {"html_url": f"{BASE}/7#issuecomment-1"}
    client.post.assert_called_once_with(
        f"{BASE}/7/comments",
        headers=HEADERS,
        json={"body": "done"},
    )


def test_close_issue_without_comment():
    patched, client = _patch_client({"number": 7, "state": "closed"})

    with patched:
        result = gitea_issues.close_issue(HOST, TOKEN, OWNER, REPO, 7)

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
        gitea_issues.close_issue(HOST, TOKEN, OWNER, REPO, 7, body="bye")

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

    result = gitea_issues._resolve_conn(args)

    assert result == (HOST, TOKEN)


def test_resolve_conn_missing_raises(monkeypatch):
    monkeypatch.delenv("GITEA_HOST", raising=False)
    monkeypatch.delenv("GITEA_ACCESS_TOKEN", raising=False)
    args = argparse.Namespace(host=None, token=None)

    with pytest.raises(SystemExit, match="GITEA_HOST"):
        gitea_issues._resolve_conn(args)


# --- main dispatch --------------------------------------------------------

_CONN = ["--host", HOST, "--token", TOKEN, "--owner", OWNER, "--repo", REPO]


def test_main_list(capsys):
    patched, _client = _patch_client(
        [{"number": 7, "title": "New room: mkt", "html_url": f"{BASE}/7"}]
    )

    with patched:
        rc = gitea_issues.main(["list", *_CONN])

    assert rc == 0
    assert "#7\tNew room: mkt" in capsys.readouterr().out


def test_main_list_empty(capsys):
    patched, _client = _patch_client([])

    with patched:
        rc = gitea_issues.main(["list", *_CONN])

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
        rc = gitea_issues.main(["show", "7", *_CONN])

    out = capsys.readouterr().out
    assert rc == 0
    assert "by: phreddy" in out
    assert "Requested by Phreddy." in out


def test_main_comment(capsys):
    patched, _client = _patch_client({"html_url": f"{BASE}/7#c1"})

    with patched:
        rc = gitea_issues.main(["comment", "7", "--body", "ok", *_CONN])

    assert rc == 0
    assert "commented on #7" in capsys.readouterr().out


def test_main_close(capsys):
    patched, _client = _patch_client({"number": 7, "state": "closed"})

    with patched:
        rc = gitea_issues.main(["close", "7", *_CONN])

    assert rc == 0
    assert "closed #7" in capsys.readouterr().out
