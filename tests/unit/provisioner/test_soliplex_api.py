"""Unit tests for ``soliplex_concierge.provisioner.soliplex_api``.

The HTTP tests replace ``urllib.request.urlopen``; minted tokens are
checked with soliplex's own ``token_mint``.
"""

import io
import json
import urllib.error

import pytest
from soliplex import authz
from soliplex import token_mint

from soliplex_concierge.provisioner import interfaces
from soliplex_concierge.provisioner import soliplex_api

BACKEND = "http://backend:8000"
SECRET = "test" * 8


class _FakeUrlopen:
    def __init__(self, *, response=None, error=None):
        self.response, self.error = response, error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


def _http_error(code, body=""):
    return urllib.error.HTTPError(
        BACKEND, code, "error", {}, io.BytesIO(body.encode())
    )


@pytest.fixture
def urlopen(monkeypatch):
    """Install a fake ``urlopen``; set its ``response`` or ``error``."""
    fake = _FakeUrlopen(response={})
    monkeypatch.setattr(soliplex_api.urllib.request, "urlopen", fake)
    return fake


# ---- secrets and tokens ----


def test_read_secret_strips_it(tmp_path):
    path = tmp_path / "url_safe_token_secret"
    path.write_text("s3cret\n")

    result = soliplex_api.read_secret(path)

    assert result == "s3cret"


def test_read_secret_unreadable(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(soliplex_api.SecretUnreadable) as caught:
        soliplex_api.read_secret(missing)

    assert caught.value.path == missing
    assert str(missing) in str(caught.value)


def test_mint_service_token():
    result = soliplex_api.mint_service_token(
        SECRET, {"sub": "soliplex-provisioner"}
    )

    claims, reason = token_mint.load_service_token(SECRET, result)
    assert reason is None
    assert claims["sub"] == "soliplex-provisioner"
    assert claims["exp"] - claims["iat"] == soliplex_api.SERVICE_TOKEN_LIFETIME


def test_mint_service_token_with_a_lifetime():
    result = soliplex_api.mint_service_token(
        SECRET, {"sub": "soliplex-provisioner"}, lifetime=60
    )

    claims, _reason = token_mint.load_service_token(SECRET, result)
    assert claims["exp"] - claims["iat"] == 60


# ---- call ----


def test_call_with_a_token(urlopen):
    urlopen.response = {"id": "test"}

    result = soliplex_api.call(
        BACKEND + "/", "GET", "/v1/installation", token="t0ken", timeout=5
    )

    assert result == {"id": "test"}
    [(request, timeout)] = urlopen.requests
    assert request.full_url == f"{BACKEND}/api/v1/installation"
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer t0ken"
    assert request.get_header("Accept") == "application/json"
    assert timeout == 5


def test_call_without_a_token(urlopen):
    result = soliplex_api.call(BACKEND, "POST", "/v1/x")

    assert result == {}
    [(request, timeout)] = urlopen.requests
    assert request.get_header("Authorization") is None
    assert request.data == b""
    assert timeout == soliplex_api.HTTP_TIMEOUT


@pytest.mark.parametrize("code", [401, 403])
def test_call_denied(urlopen, code):
    urlopen.error = _http_error(code)

    with pytest.raises(soliplex_api.AccessDenied) as caught:
        soliplex_api.call(BACKEND, "GET", "/v1/installation", token="t")

    assert caught.value.code == code
    assert caught.value.url == f"{BACKEND}/api/v1/installation"
    assert f"(HTTP {code})" in str(caught.value)


@pytest.mark.parametrize(
    "error, detail",
    [
        (
            _http_error(409, '{"detail": "workers"}'),
            'HTTP 409: {"detail": "workers"}',
        ),
        (urllib.error.URLError("refused"), "refused"),
    ],
)
def test_call_failed(urlopen, error, detail):
    urlopen.error = error

    with pytest.raises(soliplex_api.RequestFailed) as caught:
        soliplex_api.call(BACKEND, "POST", "/v1/installation/reload/rooms")

    assert caught.value.detail == detail


# ---- the installation API ----


def test_check_access(urlopen):
    urlopen.response = {"id": "test"}

    result = soliplex_api.check_access(BACKEND, token="t0ken")

    assert result is None
    [(request, _timeout)] = urlopen.requests
    assert request.full_url == f"{BACKEND}/api/v1/installation"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer t0ken"


def test_reload_rooms(urlopen):
    delta = {"added": ["alice:q3-plans"], "removed": [], "changed": []}
    urlopen.response = delta

    result = soliplex_api.reload_rooms(BACKEND)

    assert result == delta
    [(request, _timeout)] = urlopen.requests
    assert request.full_url == f"{BACKEND}/api/v1/installation/reload/rooms"
    assert request.get_method() == "POST"


@pytest.mark.parametrize(
    "delta, expected",
    [
        ({"added": ["a:b"]}, True),
        ({"changed": ["a:b"]}, True),
        ({"added": ["a:c"], "removed": ["a:b"]}, False),
        ({}, False),
    ],
)
def test_loaded(delta, expected):
    result = soliplex_api.loaded(delta, "a:b")

    assert result is expected


# ---- validate_service_token ----


def test_validate_service_token():
    token = token_mint.mint_service_token(
        SECRET, {"sub": "alice-sub"}, expires_in=60
    )

    result = soliplex_api.validate_service_token(SECRET, token)

    assert result["sub"] == "alice-sub"


@pytest.mark.parametrize(
    "secret, token",
    [
        (SECRET, "not-a-token"),
        ("other" * 8, None),
    ],
    ids=["garbage", "wrong-secret"],
)
def test_validate_service_token_refuses(secret, token):
    if token is None:
        token = token_mint.mint_service_token(
            SECRET, {"sub": "alice-sub"}, expires_in=60
        )

    with pytest.raises(soliplex_api.InvalidServiceToken) as caught:
        soliplex_api.validate_service_token(secret, token)

    assert isinstance(caught.value, interfaces.InvalidToken)
    assert caught.value.reason
    assert "the service token is not valid" in str(caught.value)


def test_access_denied_is_an_access_refusal():
    result = soliplex_api.AccessDenied(f"{BACKEND}/api/x", 403)

    assert isinstance(result, interfaces.AccessRefused)


# ---- registered_predicates ----


def test_registered_predicates(monkeypatch):
    extensions = authz.the_jsonpath_environment.function_extensions
    monkeypatch.setitem(extensions, "is_foreman", lambda claims: claims)

    result = soliplex_api.registered_predicates()

    assert "is_foreman" in result
    assert not result & authz.BUILTIN_JSONPATH_FUNCTION_NAMES
