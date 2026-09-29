"""Tests for ``stint.dialects.jira.common`` (paginate, payload parsers).

Pagination is the cross-cutting helper every admin endpoint flows through.
Its guards -- raising on an unrecognized envelope and refusing to advance
to a page whose ``startAt`` does not match what we asked for -- exist so a
subtle server-side regression doesn't manifest as an infinite loop or a
silent miss during reflect. These tests pin both behaviors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable

import httpx
import pytest
import respx

from stint.client.auth import APITokenAuth
from stint.client.http import JiraHTTPClient
from stint.dialects.jira import common
from stint.exceptions import ReflectionError

BASE = "https://jira.example.com"
TEST_PATH = "/rest/api/3/widgets"


def _client() -> JiraHTTPClient:
    return JiraHTTPClient(BASE, auth=APITokenAuth(email="you@example.com", token="tok"))


def _paginated(values: list[dict], *, start_at: int = 0, is_last: bool = True) -> dict:
    """Single-page response shape used by Jira admin endpoints."""
    return {
        "values": values,
        "isLast": is_last,
        "startAt": start_at,
        "maxResults": len(values),
        "total": len(values),
    }


def _collect(async_iter: AsyncIterator[dict]) -> Awaitable[list[dict]]:
    """Drain an async iterator into a list for assertion convenience.

    Returns the coroutine directly so callers ``await _collect(...)`` as
    if it were an async helper -- keeps test bodies linear.
    """

    async def _drain() -> list[dict]:
        out: list[dict] = []
        async for item in async_iter:
            out.append(item)
        return out

    return _drain()


@pytest.mark.asyncio
@respx.mock
async def test_paginate_unrecognized_envelope_raises():
    """A dict response without ``values`` is a contract we cannot reason
    about; treat it as a reflection failure instead of silently returning
    zero items (which would leave a live resource invisible to diff)."""
    respx.get(f"{BASE}{TEST_PATH}", params={"startAt": "0", "maxResults": "50"}).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(ReflectionError) as exc_info:
        await _collect(common.paginate(_client(), TEST_PATH))
    assert "unrecognized pagination envelope" in str(exc_info.value)
    assert TEST_PATH in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_paginate_echoed_startat_mismatch_raises():
    """If the server echoes a ``startAt`` different from what we requested,
    advancing our counter would loop on the same page. Surface this as a
    reflection failure rather than burning CPU on a stuck page."""
    route_first = respx.get(f"{BASE}{TEST_PATH}").mock(
        return_value=httpx.Response(
            200,
            json=_paginated([{"id": "a"}], start_at=50, is_last=False),
        )
    )
    route_second = respx.get(f"{BASE}{TEST_PATH}").mock(
        return_value=httpx.Response(
            200,
            json=_paginated([{"id": "b"}], start_at=50, is_last=False),
        )
    )

    with pytest.raises(ReflectionError) as exc_info:
        await _collect(common.paginate(_client(), TEST_PATH))
    # Server echoed startAt=50 on the second page even though we asked for 51;
    # guard catches it instead of looping.
    assert "echoed startAt=50" in str(exc_info.value)
    assert route_first.called
    assert route_second.called


@pytest.mark.asyncio
@respx.mock
async def test_paginate_walks_multiple_pages_with_correct_startat():
    """Sanity: the happy path still works after the new guards. Page one
    echoes startAt=0; page two echoes startAt=50; both clear isLast=False
    so the loop advances, and we get both batches."""
    respx.get(f"{BASE}{TEST_PATH}").mock(
        side_effect=[
            httpx.Response(
                200,
                json=_paginated([{"id": "a"}, {"id": "b"}], start_at=0, is_last=False),
            ),
            httpx.Response(
                200,
                json=_paginated([{"id": "c"}], start_at=2, is_last=True),
            ),
        ]
    )
    items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["a", "b", "c"]


@pytest.mark.asyncio
@respx.mock
async def test_paginate_bare_list_is_single_page():
    """Endpoints that ignore ``startAt`` and return a bare list (e.g.
    ``/issuetype``) get all items in one shot and the iterator ends."""
    respx.get(f"{BASE}{TEST_PATH}").mock(return_value=httpx.Response(200, json=[{"id": "x"}, {"id": "y"}]))
    items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["x", "y"]
