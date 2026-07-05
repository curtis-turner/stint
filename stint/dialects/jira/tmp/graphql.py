"""GraphQL transport for TMP's GraphQL-shaped internal surfaces.

Two distinct endpoints speak hand-written, named GraphQL over the same token
auth already configured on ``JiraHTTPClient``: the fields gateway
(``/gateway/api/graphql``, gated per-field by ``@optIn`` directives and, for
some fields, an additional ``X-ExperimentalApi`` header) and the work-type
layout reader (``/rest/gira/1/``, no gating at all). This client owns just the
POST + the ``errors[]`` handling shared by both; each caller supplies its own
query text, operation name, variables, and any extra headers.

The gateway requires every request to name its operation, both in the query
text (``query Name { ... }`` / ``mutation Name { ... }``) and as an explicit
``operationName`` body field -- an anonymous operation now gets rejected
outright ("must be provided ... to augment observability"), confirmed live
2026-07-05. Both this and a populated ``errors`` array come back as HTTP 200,
not an HTTP error status, so ``JiraHTTPClient``'s status-code mapping alone
doesn't catch them; this layer checks the body explicitly.
"""

from __future__ import annotations

from typing import Any

from stint.client.http import JiraHTTPClient
from stint.exceptions import TmpApiError

FIELDS_GATEWAY_PATH = "/gateway/api/graphql"
LAYOUT_READ_PATH = "/rest/gira/1/"


class GraphQLClient:
    """POSTs a hand-written, named GraphQL query and raises on ``errors``."""

    def __init__(self, client: JiraHTTPClient) -> None:
        self._client = client

    async def query(
        self,
        path: str,
        *,
        query: str,
        operation_name: str,
        variables: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "operationName": operation_name}
        if variables is not None:
            payload["variables"] = variables
        body = await self._client.post_json(path, json=payload, headers=extra_headers)
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            raise TmpApiError(f"{operation_name} ({path}) returned GraphQL errors: {errors!r}")
        return body["data"]
