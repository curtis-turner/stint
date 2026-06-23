"""Auth modes for Jira.

DC: PAT (preferred), Basic (legacy).
Cloud: APIToken (preferred, email + token via Basic), OAuth2 (deferred).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class Auth(Protocol):
    """Anything that knows how to turn itself into an httpx.Auth."""

    def to_httpx_auth(self) -> httpx.Auth: ...


class _BearerAuth(httpx.Auth):
    """Authorization: Bearer <token>."""

    requires_request_body = False
    requires_response_body = False

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


@dataclass(frozen=True)
class PATAuth:
    """Personal Access Token, the recommended mode for Jira DC."""

    token: str

    def to_httpx_auth(self) -> httpx.Auth:
        return _BearerAuth(self.token)


@dataclass(frozen=True)
class BasicAuth:
    """Username + password. Legacy DC mode. Often disabled by admins."""

    username: str
    password: str

    def to_httpx_auth(self) -> httpx.Auth:
        return httpx.BasicAuth(self.username, self.password)


@dataclass(frozen=True)
class APITokenAuth:
    """Email + API token, the recommended mode for Jira Cloud.

    Wire format is HTTP Basic auth with the email as username.
    """

    email: str
    token: str

    def to_httpx_auth(self) -> httpx.Auth:
        return httpx.BasicAuth(self.email, self.token)
