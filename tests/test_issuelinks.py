"""Data plane: issue links (create, list, delete) — issue #48."""

from __future__ import annotations

import json
import sys

import httpx
import pytest
import respx

from stint import AsyncSession, PATAuth, StateFile, create_engine
from stint.engine import Engine
from stint.exceptions import ConfigurationError, TransportError
from stint.registry import registry
from stint.state.file import (
    ProjectMapping,
    SimpleMapping,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


def _state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetypes["ticket"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


def _issuelinks_payload(links: list[dict]) -> dict:
    return {"key": "PLAT-1", "fields": {"issuelinks": links}}


def _outward_blocks(link_id: str, other_key: str) -> dict:
    """PLAT-1 (the read issue) blocks ``other_key``."""
    return {
        "id": link_id,
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "outwardIssue": {"key": other_key, "fields": {"summary": "x"}},
    }


def _inward_blocks(link_id: str, other_key: str) -> dict:
    """``other_key`` blocks PLAT-1 (the read issue)."""
    return {
        "id": link_id,
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "inwardIssue": {"key": other_key, "fields": {"summary": "x"}},
    }


# ── link ─────────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_link_posts_type_with_outward_and_inward_keys():
    """Jira's create semantics are counter-intuitive: the issue under
    ``inwardIssue`` ends up carrying the link type's outward description
    ("blocks"). ``link(A, B)`` means "A blocks B", so A goes under
    ``inwardIssue`` and B under ``outwardIssue`` — the reverse body created
    "B blocks A" on a live tenant (2026-11)."""
    # No existing links: the pre-check GET must come back empty.
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(200, json=_issuelinks_payload([])))
    route = respx.post(f"{CLOUD_ROOT}/issueLink").mock(return_value=httpx.Response(201))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.link("PLAT-1", "PLAT-2", link_type="Blocks")
    body = json.loads(route.calls[0].request.read())
    assert body["type"] == {"name": "Blocks"}
    assert body["inwardIssue"] == {"key": "PLAT-1"}  # the blocker carries the outward description
    assert body["outwardIssue"] == {"key": "PLAT-2"}


@respx.mock
@pytest.mark.asyncio
async def test_link_round_trips_to_outward_direction_on_the_read_side():
    """Create then read back: PLAT-1's own issuelinks entry must report the
    outward direction with PLAT-2 as the other key, matching what the Jira
    UI shows after the create."""
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        side_effect=[
            # pre-check: no existing links
            httpx.Response(200, json=_issuelinks_payload([])),
            # read-back: Jira stored PLAT-1 as the blocker (outward end)
            httpx.Response(200, json=_issuelinks_payload([_outward_blocks("10000", "PLAT-2")])),
        ]
    )
    respx.post(f"{CLOUD_ROOT}/issueLink").mock(return_value=httpx.Response(201))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.link("PLAT-1", "PLAT-2", link_type="Blocks")
        links = await session.links("PLAT-1")
    assert [(x.type, x.direction, x.other_key) for x in links] == [("Blocks", "outward", "PLAT-2")]


@respx.mock
@pytest.mark.asyncio
async def test_link_is_a_noop_when_the_pair_already_exists():
    """Idempotency: re-deriving dependencies every sync pass must not
    re-POST links that are already there."""
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(200, json=_issuelinks_payload([_outward_blocks("10000", "PLAT-2")]))
    )
    post = respx.post(f"{CLOUD_ROOT}/issueLink").mock(return_value=httpx.Response(201))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.link("PLAT-1", "PLAT-2", link_type="Blocks")
    assert not post.called


@respx.mock
@pytest.mark.asyncio
async def test_link_creates_the_reverse_direction():
    """PLAT-2 blocks PLAT-1 exists; PLAT-1 blocking PLAT-2 is a NEW link."""
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(200, json=_issuelinks_payload([_inward_blocks("10000", "PLAT-2")]))
    )
    post = respx.post(f"{CLOUD_ROOT}/issueLink").mock(return_value=httpx.Response(201))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.link("PLAT-1", "PLAT-2", link_type="Blocks")
    assert post.called


@respx.mock
@pytest.mark.asyncio
async def test_link_tolerates_a_duplicate_race_as_done():
    """Another writer created the link between our pre-check and our POST;
    Jira's 'already' 400 is the state we wanted."""
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(200, json=_issuelinks_payload([])))
    respx.post(f"{CLOUD_ROOT}/issueLink").mock(
        return_value=httpx.Response(
            400,
            json={"errors": {"issueLinks": "You already have a 'Blocks' link between these issues."}},
        )
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.link("PLAT-1", "PLAT-2", link_type="Blocks")  # must not raise


@respx.mock
@pytest.mark.asyncio
async def test_link_surfaces_other_400s():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(200, json=_issuelinks_payload([])))
    respx.post(f"{CLOUD_ROOT}/issueLink").mock(
        return_value=httpx.Response(400, json={"errors": {"issueLinks": "Link type 'Blocks' is missing."}})
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(TransportError):
            await session.link("PLAT-1", "PLAT-2", link_type="Blocks")


@respx.mock
@pytest.mark.asyncio
async def test_link_refuses_a_self_link():
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(ConfigurationError, match="itself"):
            await session.link("PLAT-1", "plat-1", link_type="Relates")


# ── links ────────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_links_parses_both_directions_from_one_get():
    route = respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json=_issuelinks_payload(
                [
                    _outward_blocks("10000", "PLAT-2"),
                    _inward_blocks("10001", "PLAT-3"),
                ]
            ),
        )
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        links = await session.links("PLAT-1")
    assert route.called
    assert route.calls[0].request.url.params["fields"] == "issuelinks"
    assert [(x.id, x.type, x.direction, x.other_key) for x in links] == [
        ("10000", "Blocks", "outward", "PLAT-2"),
        ("10001", "Blocks", "inward", "PLAT-3"),
    ]


# ── unlink ───────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_unlink_deletes_by_link_id():
    route = respx.delete(f"{CLOUD_ROOT}/issueLink/10000").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.unlink("10000")
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_link_round_trip_list_then_unlink():
    """The report's audit flow: read links back, delete the stale ones."""
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(200, json=_issuelinks_payload([_outward_blocks("10000", "PLAT-2")]))
    )
    delete = respx.delete(f"{CLOUD_ROOT}/issueLink/10000").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        links = await session.links("PLAT-1")
        for link in links:
            if link.other_key == "PLAT-2":
                await session.unlink(link.id)
    assert delete.called
