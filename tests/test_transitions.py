"""Data plane: workflow transitions (session.transition / available_transitions)
plus the derived `status_category` hydration."""

from __future__ import annotations

import sys

import httpx
import pytest
import respx

from stint import (
    AsyncSession,
    PATAuth,
    StateFile,
    create_engine,
)
from stint.engine import Engine
from stint.exceptions import ConfigurationError, TransitionError
from stint.query.hydrate import field_keys_for_model, hydrate
from stint.query.payload import build_fields_payload
from stint.registry import registry
from stint.schema.issuetype import IssueType
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


class Ticket(IssueType):
    __alias__ = "ticket"
    summary: str
    status: str | None = None
    status_category: str | None = None


def _issue_payload(status: str = "In Progress", category: str = "indeterminate") -> dict:
    return {
        "key": "PLAT-1",
        "fields": {
            "summary": "boom",
            "status": {
                "name": status,
                "statusCategory": {"key": category, "name": "In Progress"},
            },
        },
    }


_TRANSITIONS = {
    "transitions": [
        {
            "id": "11",
            "name": "Done",
            "to": {"name": "Done", "statusCategory": {"key": "done"}},
        },
        {
            "id": "31",
            "name": "Reopen",
            "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        },
        {
            "id": "41",
            "name": "Hold",
            "to": {"name": "On Hold", "statusCategory": {"key": "new"}},
        },
    ]
}


# ── Hydration: status_category ───────────────────────────────────────
def test_status_category_hydrates_from_status_payload():
    ticket = hydrate(Ticket, _issue_payload(), _state())
    assert ticket.status == "In Progress"
    assert ticket.status_category == "indeterminate"


def test_status_category_missing_status_leaves_default():
    payload = {"key": "PLAT-1", "fields": {"summary": "boom"}}
    ticket = hydrate(Ticket, payload, _state())
    assert ticket.status is None
    assert ticket.status_category is None


def test_field_keys_map_status_category_to_status_once():
    keys = field_keys_for_model(Ticket, _state())
    assert keys == ["summary", "status"]  # not "status_category", no duplicate


def test_payload_builder_skips_status_category():
    ticket = Ticket(summary="boom", status="Done", status_category="done")
    fields = build_fields_payload(ticket, _state(), is_cloud=True)
    assert "status_category" not in fields


# ── available_transitions ────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_available_transitions_returns_named_triples():
    route = respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(
        return_value=httpx.Response(200, json=_TRANSITIONS)
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        transitions = await session.available_transitions("PLAT-1")
    assert route.called
    assert len(transitions) == 3
    assert transitions[0].id == "11"
    assert transitions[0].name == "Done"
    assert transitions[0].to_status == "Done"
    # Tuple-style unpacking works too.
    ids, names, statuses = zip(*transitions, strict=True)
    assert ids == ("11", "31", "41")
    assert "Done" in names
    assert set(statuses) == {"Done", "In Progress", "On Hold"}


# ── transition() ─────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_transition_by_status_name_posts_transition_id():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    post = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.transition("PLAT-1", to_status="Done")
    assert post.called
    import json as _json

    body = _json.loads(post.calls[0].request.read())
    assert body["transition"] == {"id": "11"}
    assert "fields" not in body  # nothing to set: fields omitted entirely


@respx.mock
@pytest.mark.asyncio
async def test_transition_by_status_name_is_case_insensitive():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    post = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.transition("PLAT-1", to_status="done")
    assert post.called


@respx.mock
@pytest.mark.asyncio
async def test_transition_by_id():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    post = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.transition("PLAT-1", transition_id="41")
    assert post.called
    assert b"41" in post.calls[0].request.read()


@respx.mock
@pytest.mark.asyncio
async def test_transition_unknown_status_raises_naming_available():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    post = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(TransitionError) as excinfo:
            await session.transition("PLAT-1", to_status="Closed")
    assert "Closed" in str(excinfo.value)
    # The error names what IS reachable so a sync loop can log it.
    assert "Done" in str(excinfo.value)
    assert "On Hold" in str(excinfo.value)
    assert not post.called


@respx.mock
@pytest.mark.asyncio
async def test_transition_unknown_id_raises():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(TransitionError, match="99"):
            await session.transition("PLAT-1", transition_id="99")


@respx.mock
@pytest.mark.asyncio
async def test_transition_to_current_status_is_a_noop():
    """Sync loops re-derive the target state every pass; moving to the
    current status must not touch the network (idempotency)."""
    async with AsyncSession(_cloud_engine(), _state()) as session:
        ticket = hydrate(Ticket, _issue_payload(status="Done", category="done"), _state())
        session._identity[(Ticket, "PLAT-1")] = ticket
        await session.transition(ticket, to_status="done")  # case-insensitive no-op
        # No HTTP at all: no routes registered, any request would raise.


@respx.mock
@pytest.mark.asyncio
async def test_transition_with_resolution_and_fields_rides_same_request():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    post = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.transition(
            "PLAT-1",
            to_status="Done",
            resolution="Fixed",
            fields={"customfield_10001": "audit note"},
        )
    import json as _json

    body = _json.loads(post.calls[0].request.read())
    assert body["transition"] == {"id": "11"}
    assert body["fields"]["resolution"] == {"name": "Fixed"}
    assert body["fields"]["customfield_10001"] == "audit note"


@respx.mock
@pytest.mark.asyncio
async def test_transition_requires_a_destination():
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(ConfigurationError, match="to_status"):
            await session.transition("PLAT-1")
        with pytest.raises(ConfigurationError, match="not both"):
            await session.transition("PLAT-1", to_status="Done", transition_id="11")


@respx.mock
@pytest.mark.asyncio
async def test_transition_instance_without_key_raises():
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(ConfigurationError, match="no `key`"):
            await session.transition(Ticket(summary="boom"), to_status="Done")


@respx.mock
@pytest.mark.asyncio
async def test_transition_refreshes_instance_status_and_baseline():
    """After a transition, a hydrated instance reads the new status — and a
    later commit() must not PUT the old status back (baseline refreshed)."""
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(200, json=_TRANSITIONS))
    respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/transitions").mock(return_value=httpx.Response(204))
    put = respx.put(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        ticket = hydrate(Ticket, _issue_payload(), _state())
        session._identity[(Ticket, "PLAT-1")] = ticket
        session._originals[(Ticket, "PLAT-1")] = ticket.model_dump()
        await session.transition(ticket, to_status="Done")
        assert ticket.status == "Done"
        assert ticket.status_category == "done"
        results = await session.commit()
        assert results == []  # nothing dirty: status refresh did not mark it
        assert not put.called
