"""Data plane: changelog / history reads — issue #51 (lower priority)."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest
import respx

from stint import AsyncSession, PATAuth, StateFile, create_engine
from stint.engine import Engine
from stint.registry import registry
from stint.state.file import (
    CustomFieldMapping,
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
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10044", options={"S1": "100"})
    state.issuetypes["ticket"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


def _history(items: list[dict], created: str = "2026-11-23T09:00:00.000+0000", author: str = "alice") -> dict:
    return {
        "id": "10000",
        "author": {"accountId": "acc-1", "displayName": author},
        "created": created,
        "items": items,
    }


def _item(field: str, **kw: Any) -> dict:
    base = {"field": field, "fieldtype": "jira"}
    base.update(kw)
    return base


# ── happy path ───────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_changelog_yields_readable_change_items():
    page = {
        "values": [
            _history(
                [
                    _item(
                        "status", **{"from": "10001", "fromString": "In Progress", "to": "10002", "toString": "Done"}
                    ),
                ]
            ),
        ],
        "startAt": 0,
        "total": 1,
        "isLast": True,
    }
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/changelog").mock(return_value=httpx.Response(200, json=page))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        changes = [change async for change in session.changelog("PLAT-1")]
    assert len(changes) == 1
    change = changes[0]
    assert change.field == "status"
    assert change.author == "alice"
    assert change.created.startswith("2026-11-23")
    assert change.from_value == "In Progress"  # readable strings preferred
    assert change.to_value == "Done"


@respx.mock
@pytest.mark.asyncio
async def test_changelog_maps_customfield_ids_to_declared_aliases():
    """Audit output speaks the schema's language, not customfield_XXXXX."""
    page = {
        "values": [
            _history(
                [
                    _item(
                        "customfield_10044",
                        fieldtype="custom",
                        **{"from": None, "fromString": None, "to": "100", "toString": "S1"},
                    ),
                    _item(
                        "customfield_99999",
                        fieldtype="custom",
                        **{"from": None, "fromString": None, "to": "7", "toString": "seven"},
                    ),
                ]
            ),
        ],
        "startAt": 0,
        "total": 1,
        "isLast": True,
    }
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/changelog").mock(return_value=httpx.Response(200, json=page))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        changes = [change async for change in session.changelog("PLAT-1")]
    # Known id -> declared alias; unknown id passes through.
    assert [c.field for c in changes] == ["bug_severity", "customfield_99999"]
    # A fresh value has from_value=None (the raw "from" key is absent, not a string).
    assert changes[0].from_value is None
    assert changes[0].to_value == "S1"


@respx.mock
@pytest.mark.asyncio
async def test_changelog_maps_cloud_display_names_via_field_id():
    """Realistic Cloud changelog items: ``field`` carries the display name
    ("Severity") and ``fieldId`` the id ("customfield_10044") — ``field``
    never starts with customfield_ on Cloud, so only the id maps back to the
    declared alias. System fields and unknown ids fall back to ``field``."""
    page = {
        "values": [
            _history(
                [
                    _item(
                        "Severity",
                        fieldtype="custom",
                        fieldId="customfield_10044",
                        **{"from": None, "fromString": None, "to": "100", "toString": "S1"},
                    ),
                    _item(
                        "Story Points",
                        fieldtype="custom",
                        fieldId="customfield_99999",
                        **{"from": "3", "fromString": "3", "to": "5", "toString": "5"},
                    ),
                    _item(
                        "status",
                        fieldId="status",
                        **{"from": "10001", "fromString": "In Progress", "to": "10002", "toString": "Done"},
                    ),
                ]
            ),
        ],
        "startAt": 0,
        "total": 1,
        "isLast": True,
    }
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/changelog").mock(return_value=httpx.Response(200, json=page))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        changes = [change async for change in session.changelog("PLAT-1")]
    # Known fieldId -> declared alias; unknown id and system fields keep the
    # display name.
    assert [c.field for c in changes] == ["bug_severity", "Story Points", "status"]
    assert changes[0].to_value == "S1"


@respx.mock
@pytest.mark.asyncio
async def test_changelog_falls_back_to_raw_ids_without_strings():
    page = {
        "values": [
            _history([_item("assignee", **{"from": "acc-1", "to": "acc-2"})]),
        ],
        "startAt": 0,
        "total": 1,
        "isLast": True,
    }
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/changelog").mock(return_value=httpx.Response(200, json=page))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        changes = [change async for change in session.changelog("PLAT-1")]
    assert changes[0].from_value == "acc-1"
    assert changes[0].to_value == "acc-2"


@respx.mock
@pytest.mark.asyncio
async def test_changelog_paginates():
    page1 = {
        "values": [_history([_item("summary", **{"fromString": "old", "toString": "new"})])],
        "startAt": 0,
        "maxResults": 1,
        "total": 2,
        "isLast": False,
    }
    page2 = {
        "values": [_history([_item("status", **{"fromString": "open", "toString": "done"})])],
        "startAt": 1,
        "maxResults": 1,
        "total": 2,
        "isLast": True,
    }
    route = respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/changelog").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        changes = [change async for change in session.changelog("PLAT-1")]
    assert [c.field for c in changes] == ["summary", "status"]
    assert route.calls[1].request.url.params["startAt"] == "1"


@respx.mock
@pytest.mark.asyncio
async def test_changelog_accepts_a_hydrated_instance():
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/changelog").mock(
        return_value=httpx.Response(200, json={"values": [], "startAt": 0, "total": 0, "isLast": True})
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        changes = [change async for change in session.changelog("PLAT-1")]
    assert changes == []
