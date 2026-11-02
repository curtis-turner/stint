"""Thin async httpx wrapper for Jira REST. Handles auth, JSON, basic error mapping.

Does not know about admin objects or JQL. The dialect calls this for raw GETs.

Retry policy:
- 429: retried for every method, up to ``max_retries`` times with
  exponential backoff (0.5s base, doubling). Honors the ``Retry-After``
  header when present.
- 503: retried only for idempotent methods (GET/PUT/DELETE). A 503 can
  arrive *after* the server committed a non-idempotent POST write, so
  retrying POSTs risks duplicating the created object.
- Transport-level failures (connection refused/reset, DNS, read timeout —
  ``httpx.RequestError``): retried with the same backoff, then re-raised
  as ``TransportError`` with ``status_code=None`` so callers can
  ``except StintError`` for every network failure path.
Non-retryable HTTP errors (401/403/404/4xx) propagate immediately.
"""

from __future__ import annotations

import asyncio
import json
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
# POST is excluded: a 503 may follow a committed write, and re-sending a
# non-idempotent body can create duplicates the state file never learns about.
IDEMPOTENT_METHODS = ("GET", "PUT", "DELETE", "HEAD")


def _default_user_agent() -> str:
    """``stint/<version>``, matching the installed release.

    Imported lazily: ``stint/__init__`` (which defines ``__version__``)
    imports the query layer, which imports this module — a top-level
    ``from stint import __version__`` here would be a circular import.
    """
    from stint import __version__

    return f"stint/{__version__}"


class JiraHTTPClient:
    """Async HTTP client for a single Jira base URL."""

    def __init__(
        self,
        base_url: str,
        auth: Auth,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        user_agent: str | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth.to_httpx_auth(),
            verify=verify_ssl,
            timeout=timeout,
            headers={
                "User-Agent": user_agent if user_agent is not None else _default_user_agent(),
                "Accept": "application/json",
            },
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

    async def delete_json(self, path: str, **kwargs: Any) -> Any:
        """DELETE whose 2xx response body is returned.

        At least one Cloud endpoint (v2 bulk field-removal) reports
        per-item failures inside a 200 body instead of an error status,
        so the body must be inspectable (M14). Empty bodies -> ``None``.
        """
        return self._unwrap(await self._send_with_retry("DELETE", path, **kwargs))

    async def _send_with_retry(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue the request, applying the documented retry policy.

        Retries: 429 always; 503 only for idempotent methods;
        transport-level ``httpx.RequestError`` for all methods (the failure
        happened before the request could complete, so re-sending cannot
        duplicate a committed write). Exhausted transport retries raise
        ``TransportError(status_code=None)`` — never a raw httpx exception.
        """
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise TransportError(
                        f"{method} {path} failed at the transport layer: {exc}",
                        status_code=None,
                    ) from exc
                await asyncio.sleep(self._transport_backoff(attempt))
                attempt += 1
                continue
            if response.status_code not in RETRYABLE_STATUSES:
                return response
            if response.status_code == 503 and method not in IDEMPOTENT_METHODS:
                return response  # let _unwrap raise; a POST retry could duplicate a committed write
            if attempt >= self._max_retries:
                return response  # let _unwrap raise TransportError
            delay = self._compute_delay(response, attempt)
            await asyncio.sleep(delay)
            attempt += 1

    def _transport_backoff(self, attempt: int) -> float:
        """Exponential backoff + jitter for connection-level retries
        (no ``Retry-After`` exists without a response)."""
        return self._backoff_base * (2**attempt) + random.uniform(0, 0.1)

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

    def close_sync(self) -> None:
        """Synchronously close the underlying pool — for paths that cannot
        ``await close()``, i.e. a constructor failure before any request ran.

        No loop-scoped state exists at that point (no request has run, the
        pool was never bound to a loop), so driving ``aclose()`` through a
        dedicated event loop is safe. Inside an already-running loop there is
        nothing to free synchronously: the pool has not opened any transport
        yet, and is left for the running loop's owner (or GC) as before.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._client.aclose())

    async def __aenter__(self) -> JiraHTTPClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _display_url(self, url: httpx.URL) -> str:
        """Endpoint identity for error messages, minus the query string.

        The path names the failed call; echoing the full query string would
        leak request parameters into logs/tracebacks — including PII such as
        a user's email passed to account-search endpoints as ``?query=``.
        """
        return str(url).split("?", 1)[0]

    def _unwrap(self, response: httpx.Response, *, expect_json: bool = True) -> Any:
        status = response.status_code
        url = self._display_url(response.request.url)
        if status == 401:
            raise AuthenticationError(f"401 from {url}: {response.text[:200]}")
        if status == 403:
            raise PermissionError(
                f"403 from {url}: likely insufficient permission. "
                f"Admin endpoints typically require Jira Site Admin or Org Admin."
            )
        if status == 404:
            raise NotFoundError(f"404 from {url}")
        if 500 <= status < 600:
            raise TransportError(
                f"{status} from {url}: {response.text[:200]}",
                status_code=status,
            )
        if status >= 400:
            raise TransportError(
                f"{status} from {url}: {response.text[:200]}",
                status_code=status,
            )
        if not expect_json:
            return None
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            # Captive portals, proxies, or a wrong base_url can answer 2xx
            # with HTML/text. Surface it as a StintError, not a raw traceback.
            raise TransportError(
                f"non-JSON response from {url}: {response.text[:200]}",
                status_code=status,
            ) from exc
