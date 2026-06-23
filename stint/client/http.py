"""Thin async httpx wrapper for Jira REST. Handles auth, JSON, basic error mapping.

Does not know about admin objects or JQL. The dialect calls this for raw GETs.

Retry policy (M4): on 429 and 503, retry up to ``max_retries`` times with
exponential backoff (0.5s base, doubling). Honors the ``Retry-After`` header
when present. Non-retryable errors (401/403/404/4xx) propagate immediately.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from stint.client.auth import Auth
from stint.exceptions import (
    AuthenticationError,
    NotFoundError,
    PermissionError,
    TransportError,
)

RETRYABLE_STATUSES = (429, 503)


class JiraHTTPClient:
    """Async HTTP client for a single Jira base URL."""

    def __init__(
        self,
        base_url: str,
        auth: Auth,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        user_agent: str = "stint/0.1",
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth.to_httpx_auth(),
            verify=verify_ssl,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        return self._unwrap(await self._send_with_retry("GET", path, **kwargs))

    async def post_json(self, path: str, *, json: Any, **kwargs: Any) -> Any:
        return self._unwrap(await self._send_with_retry("POST", path, json=json, **kwargs))

    async def put_json(self, path: str, *, json: Any, **kwargs: Any) -> Any:
        return self._unwrap(await self._send_with_retry("PUT", path, json=json, **kwargs))

    async def delete(self, path: str, **kwargs: Any) -> None:
        self._unwrap(
            await self._send_with_retry("DELETE", path, **kwargs),
            expect_json=False,
        )

    async def _send_with_retry(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue the request, retrying on 429/503 up to max_retries."""
        attempt = 0
        while True:
            response = await self._client.request(method, path, **kwargs)
            if response.status_code not in RETRYABLE_STATUSES:
                return response
            if attempt >= self._max_retries:
                return response  # let _unwrap raise TransportError
            delay = self._compute_delay(response, attempt)
            await asyncio.sleep(delay)
            attempt += 1

    def _compute_delay(self, response: httpx.Response, attempt: int) -> float:
        """Honor Retry-After when present; otherwise exponential backoff + jitter."""
        ra = response.headers.get("Retry-After")
        if ra is not None:
            try:
                return float(ra)
            except ValueError:
                pass  # HTTP-date format unsupported; fall through
        return self._backoff_base * (2**attempt) + random.uniform(0, 0.1)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> JiraHTTPClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _unwrap(self, response: httpx.Response, *, expect_json: bool = True) -> Any:
        status = response.status_code
        if status == 401:
            raise AuthenticationError(f"401 from {response.request.url}: {response.text[:200]}")
        if status == 403:
            raise PermissionError(
                f"403 from {response.request.url}: likely insufficient permission. "
                f"Admin endpoints typically require Site Admin (DC) or Org Admin (Cloud)."
            )
        if status == 404:
            raise NotFoundError(f"404 from {response.request.url}")
        if 500 <= status < 600:
            raise TransportError(
                f"{status} from {response.request.url}: {response.text[:200]}",
                status_code=status,
            )
        if status >= 400:
            raise TransportError(
                f"{status} from {response.request.url}: {response.text[:200]}",
                status_code=status,
            )
        if not expect_json:
            return None
        if not response.content:
            return None
        return response.json()
