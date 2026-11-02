"""Tests for ``stint.dialects.jira.common`` (paginate, payload parsers).

Pagination is the cross-cutting helper every admin endpoint flows through.
Its guards -- raising on an unrecognized envelope and refusing to advance
to a page whose ``startAt`` does not match what we asked for -- exist so a
subtle server-side regression doesn't manifest as an infinite loop or a
silent miss during reflect. These tests pin both behaviors.
"""

from __future__ import annotations

import warnings
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
async def test_paginate_forward_echo_mismatch_aligns_with_warning():
    """Atlassian's experimental v2 Field Schemes endpoints have been observed
    echoing a ``startAt`` the client never requested (e.g. request startAt=0,
    echo startAt=50). The previous hard-abort made such tenants
    unreflectable. Policy: refetch to rule out a transient glitch, then — if
    persistent — warn once, align to the server's echo, and terminate
    normally (no raise, no hang, no re-served pages)."""
    with pytest.warns(UserWarning, match="echoes startAt=50"):
        # The endpoint always answers page "50" regardless of what we ask:
        # after alignment (start=50) the next response must be either in sync
        # or clamped (echo < start) — here we serve an in-sync echo so the
        # page advances and the remainder clamps out on the following fetch.
        respx.get(f"{BASE}{TEST_PATH}").mock(
            side_effect=[
                httpx.Response(  # requested startAt=0, echo insists 50 (glitch #1 in refetch loop)
                    200,
                    json=_paginated([{"id": "a"}], start_at=50, is_last=True),
                ),
                httpx.Response(  # refetch, still insisting
                    200,
                    json=_paginated([{"id": "a"}], start_at=50, is_last=True),
                ),
                httpx.Response(  # refetch, still insisting
                    200,
                    json=_paginated([{"id": "a"}], start_at=50, is_last=True),
                ),
            ]
        )
        items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["a"]  # yielded once, no duplicates


@pytest.mark.asyncio
@respx.mock
async def test_paginate_transient_echo_glitch_refetches_and_recovers():
    """When the refetch comes back in sync, the glitch is transient: the
    pipeline proceeds without warning, exactly as if nothing happened."""
    respx.get(f"{BASE}{TEST_PATH}").mock(
        side_effect=[
            httpx.Response(  # requested startAt=0, echo race says 50
                200,
                json=_paginated([{"id": "a"}], start_at=50, is_last=False),
            ),
            httpx.Response(  # refetch #1: back in sync
                200,
                json=_paginated([{"id": "a"}], start_at=0, is_last=False),
            ),
            httpx.Response(  # requested startAt=1 (advanced past "a")
                200,
                json=_paginated([{"id": "b"}], start_at=1, is_last=True),
            ),
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any pagination warning = failure
        items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["a", "b"]


@pytest.mark.asyncio
@respx.mock
async def test_paginate_echo_behind_start_is_clamped_to_end():
    """An echo behind the requested start means the server served a page we
    already consumed (out-of-range startAt clamp): treat as end-of-pages
    rather than re-serving duplicates."""
    respx.get(f"{BASE}{TEST_PATH}").mock(
        side_effect=[
            httpx.Response(  # page 1, 50 entries-worth ("a"), no isLast
                200,
                json=_paginated([{"id": "a"}, {"id": "b"}], start_at=0, is_last=False),
            ),
            httpx.Response(  # requested startAt=2, server clamps to 0
                200,
                json=_paginated([{"id": "a"}], start_at=0, is_last=False),
            ),
        ]
    )
    items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["a", "b"]


@pytest.mark.asyncio
@respx.mock
async def test_paginate_runaway_pager_is_bounded(monkeypatch):
    """The _MAX_PAGES cap: an endpoint that keeps serving fresh pages with no
    end marker must raise instead of spinning the generator forever."""
    monkeypatch.setattr(common, "_MAX_PAGES", 4)
    # echo == start forever (an honest-looking pager), values never empty,
    # isLast never set: the loop advances ``start`` each round forever.
    respx.get(f"{BASE}{TEST_PATH}").mock(
        side_effect=[
            httpx.Response(
                200,
                json=_paginated([{"id": f"i{i}"}], start_at=i, is_last=False),
            )
            for i in range(6)
        ]
    )
    with pytest.raises(ReflectionError, match="exceeded 4 pages"):
        await _collect(common.paginate(_client(), TEST_PATH))


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


@pytest.mark.asyncio
@respx.mock
async def test_paginate_bare_envelope_without_pagination_metadata_is_single_page():
    """A non-paginated dict envelope (``/issuesecurityschemes`` shape:
    ``{"issueSecuritySchemes": [...]}`` — no ``startAt``, no ``isLast``)
    must terminate after one request. Before the bare-envelope guard, the
    loop re-requested it with ``startAt=50``, the endpoint ignored that and
    served the identical page again (nonempty values, no ``isLast``), and
    paginate spun forever — the silent ``revision --autogenerate`` hang.

    Only truly paginated responses carry the metadata needed to advance;
    an envelope with none is definitionally a single page."""
    respx.get(f"{BASE}{TEST_PATH}").mock(
        return_value=httpx.Response(200, json={"issueSecuritySchemes": [{"id": "s1"}, {"id": "s2"}]})
    )
    items = await _collect(common.paginate(_client(), TEST_PATH, envelope_key="issueSecuritySchemes"))
    assert [item["id"] for item in items] == ["s1", "s2"]
    assert respx.get(f"{BASE}{TEST_PATH}").call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_paginate_follows_next_page_cursor():
    """L1: the docstring promised ``nextPage`` support it did not implement.
    A cursor-paginated endpoint (one that ignores ``startAt`` and hands back
    a ``nextPage`` URL) must be walked via the server-provided URL, not via
    ``startAt`` — which would otherwise yield duplicates forever; the
    startAt-echo guard does not fire because cursor endpoints echo what we
    sent."""
    page_two = f"{BASE}/rest/api/3/widgets/cursor2"
    respx.get(f"{BASE}{TEST_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"id": "a"}], "startAt": 0, "nextPage": page_two},
        )
    )
    cursor_route = respx.get(page_two).mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"id": "b"}]},
        )
    )
    items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["a", "b"]
    assert cursor_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_paginate_self_referential_next_page_raises():
    """A cursor that keeps pointing at itself must not loop forever yielding
    the same items. It is verified across refetches first (stale-envelope
    glitches resolve; observed on the v2 Field Schemes endpoints), and only
    a persistently self-referential cursor raises."""
    page_two = f"{BASE}/rest/api/3/widgets/pages/two"  # distinct path: avoids respx same-path/param ambiguity
    respx.get(f"{BASE}{TEST_PATH}").mock(
        return_value=httpx.Response(200, json={"values": [{"id": "a"}], "startAt": 0, "nextPage": page_two})
    )
    cursor_route = respx.get(page_two).mock(
        return_value=httpx.Response(200, json={"values": [{"id": "a"}], "startAt": 0, "nextPage": page_two})
    )
    with pytest.raises(ReflectionError, match="self-referential cursor"):
        await _collect(common.paginate(_client(), TEST_PATH))
    # 1 follow + _ECHO_MISMATCH_REFETCHES (2) verification fetches:
    assert cursor_route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_paginate_self_referential_next_page_that_resolves_recovers():
    """A cursor answered once by a stale envelope (pointing at itself, with
    the previous page's rows inside) that stops repeating on refetch is
    transient: the recovered response is processed normally and reflection
    completes."""
    page_two = f"{BASE}/rest/api/3/widgets/pages/two"
    stale = {"values": [{"id": "a"}], "startAt": 0, "nextPage": page_two}
    respx.get(f"{BASE}{TEST_PATH}").mock(
        return_value=httpx.Response(200, json={"values": [{"id": "a"}], "startAt": 0, "nextPage": page_two})
    )
    cursor_route = respx.get(page_two).mock(
        side_effect=[
            httpx.Response(200, json=stale),  # follow: stale self-reference
            httpx.Response(  # 1st verification refetch: cursor moved
                200,
                json={"values": [{"id": "b"}], "startAt": 1, "isLast": True},
            ),
        ]
    )
    items = await _collect(common.paginate(_client(), TEST_PATH))
    # The stale envelope carried page-1's rows a second time ('a' twice) —
    # what the real stale-glitch serves; "b" arrives after recovery. Callers
    # dedupe rows by field id (items dicts), so this is the documented cost.
    assert [item["id"] for item in items] == ["a", "a", "b"]
    assert cursor_route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_paginate_clamped_startat_echo_is_end_of_pages():
    """L4: a server may clamp an out-of-range ``startAt`` back to a page it
    already served (e.g. when the item count is not a page-size multiple).
    That echo previously raised mid-iteration; it must terminate the
    iteration instead of aborting a working reflect."""
    respx.get(f"{BASE}{TEST_PATH}").mock(
        side_effect=[
            httpx.Response(200, json=_paginated([{"id": "a"}, {"id": "b"}], start_at=0, is_last=False)),
            # Client asks startAt=2; server clamps back to 0 and re-serves page 1.
            httpx.Response(200, json=_paginated([{"id": "a"}, {"id": "b"}], start_at=0, is_last=False)),
        ]
    )
    items = await _collect(common.paginate(_client(), TEST_PATH))
    assert [item["id"] for item in items] == ["a", "b"]  # no duplicates, no raise
