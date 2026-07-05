"""Internal REST transport for TMP's non-GraphQL internal surfaces.

Two path families, both token-reachable with the same auth as the rest of
stint: simplified REST for work types (``/rest/internal/simplified/1.0/...``)
and the issue-layout endpoint for work-type update + field association
(``/rest/internal/1.0/issueLayouts/...``). Both send
``X-Atlassian-Token: no-check`` -- confirmed required for the simplified-REST
writes, and sent defensively on the layout PUT too (the browser capture that
proved the PUT works over token auth didn't clarify whether it's strictly
required there, and sending it costs nothing).
"""

from __future__ import annotations

from typing import Any

from stint.client.http import JiraHTTPClient

SIMPLIFIED_PATH = "/rest/internal/simplified/1.0"
ISSUE_LAYOUTS_PATH = "/rest/internal/1.0/issueLayouts"

_HEADERS = {"X-Atlassian-Token": "no-check"}


class InternalRestClient:
    """Thin wrapper sending ``X-Atlassian-Token: no-check`` on every write."""

    def __init__(self, client: JiraHTTPClient) -> None:
        self._client = client

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._client.get_json(path, **kwargs)

    async def post(self, path: str, *, json: Any) -> Any:
        return await self._client.post_json(path, json=json, headers=_HEADERS)

    async def put(self, path: str, *, json: Any) -> Any:
        return await self._client.put_json(path, json=json, headers=_HEADERS)

    async def delete(self, path: str) -> None:
        await self._client.delete(path, headers=_HEADERS)
