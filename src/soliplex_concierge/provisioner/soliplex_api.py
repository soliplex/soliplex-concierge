"""Call a Soliplex backend's installation API, as a provisioner.

A provisioner authenticates as itself, with a short-lived soliplex service
token (:func:`mint_service_token`) signed with the installation's
``URL_SAFE_TOKEN_SECRET`` (:func:`read_secret` reads it from a file). The
backend admits the token when an ``admin_users`` entry matches its claims.
A backend with no OIDC provider takes a call without a token as its no-auth
user's, so ``token`` is optional throughout.
"""

import collections.abc
import json
import pathlib
import urllib.error
import urllib.request

from soliplex import token_mint

SERVICE_TOKEN_LIFETIME = 300
HTTP_TIMEOUT = 30


class BackendError(Exception):
    """A failure talking to the backend."""


class SecretUnreadable(BackendError):
    def __init__(self, path):
        self.path = path
        super().__init__(f"cannot read the signing secret from {path}")


class AccessDenied(BackendError):
    """The backend refused the provisioner's credentials (401 / 403)."""

    def __init__(self, url, code):
        self.url, self.code = url, code
        super().__init__(
            f"the backend refused the provisioner's access to {url} "
            f"(HTTP {code})"
        )


class RequestFailed(BackendError):
    def __init__(self, url, detail):
        self.url, self.detail = url, detail
        super().__init__(f"backend request {url} failed: {detail}")


def read_secret(path: pathlib.Path) -> str:
    """The signing secret in the file at ``path``, stripped.

    Raises :class:`SecretUnreadable` when the file cannot be read.
    """
    try:
        return path.read_text().strip()
    except OSError as error:
        raise SecretUnreadable(path) from error


def mint_service_token(
    secret: str,
    claims: collections.abc.Mapping,
    *,
    lifetime: int = SERVICE_TOKEN_LIFETIME,
) -> str:
    """A service token holding ``claims``, valid for ``lifetime`` seconds."""
    return token_mint.mint_service_token(
        secret, dict(claims), expires_in=lifetime
    )


def call(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> dict:
    """Call the backend's API at ``path`` (under ``/api``); return its JSON.

    Raises :class:`AccessDenied` when the backend refuses the credentials,
    and :class:`RequestFailed` for any other failure.
    """
    url = f"{base_url.rstrip('/')}/api{path}"
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AccessDenied(url, error.code) from error
        body = error.read().decode(errors="replace")
        raise RequestFailed(url, f"HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RequestFailed(url, error.reason) from error


def check_access(base_url: str, *, token: str | None = None) -> None:
    """Raise :class:`AccessDenied` unless the backend admits ``token``.

    That is, unless it lets the token's holder read the installation, which
    takes an admin.
    """
    call(base_url, "GET", "/v1/installation", token=token)


def reload_rooms(base_url: str, *, token: str | None = None) -> dict:
    """Have the backend reload its room configs; return the delta.

    The delta lists room ids under ``added``, ``removed`` and ``changed``.
    """
    return call(base_url, "POST", "/v1/installation/reload/rooms", token=token)


def loaded(delta: collections.abc.Mapping, room_id: str) -> bool:
    """Did the rooms reload ``delta`` load (add or change) ``room_id``?"""
    return room_id in (*delta.get("added", ()), *delta.get("changed", ()))
