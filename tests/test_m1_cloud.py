"""Jira Cloud dialect: detect, /rest/api/3 paths, field-context option lookup."""

from typing import Any

import httpx
import pytest
import respx

from stint import APITokenAuth, create_engine

BASE = "https://acme.atlassian.net"
CLOUD_ROOT = f"{BASE}/rest/api/3"


def _cloud_engine():
    return create_engine(
        f"jira_cloud+{BASE}",
        auth=APITokenAuth(email="user@acme.com", token="api-tok"),
    )


def _paginated(values: list[Any]) -> dict[str, Any]:
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def _stub_empty(mock: respx.MockRouter) -> None:
    mock.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "1001.0.0",
                "deploymentType": "Cloud",
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/field").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{CLOUD_ROOT}/project/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/screens").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/screenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescheme/mapping").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescreenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescreenscheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/fieldconfiguration").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/fieldconfigurationscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/fieldconfigurationscheme/project").mock(
        return_value=httpx.Response(200, json=_paginated([]))
    )


# ── Detection ────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_cloud_detect_true_for_cloud_server():
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "1001.0.0",
                "deploymentType": "Cloud",
            },
        )
    )
    async with _cloud_engine() as eng:
        assert await eng.detect() is True


@pytest.mark.asyncio
@respx.mock
async def test_cloud_detect_false_for_dc_server():
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "9.12.4",
                "deploymentType": "Server",
            },
        )
    )
    async with _cloud_engine() as eng:
        assert await eng.detect() is False


# ── Cloud-specific: option fetch via field context ───────────────────
@pytest.mark.asyncio
@respx.mock
async def test_cloud_select_options_fetched_via_default_context():
    """Cloud option lookup: list contexts, take the first, list its options."""
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "customfield_10501",
                    "name": "Severity",
                    "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
                },
            ],
        )
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10501/context").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "20000", "name": "Default Context", "isGlobalContext": True},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10501/context/20000/option").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "30001", "value": "S1"},
                    {"id": "30002", "value": "S2"},
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    sev = snap.custom_fields["customfield_10501"]
    assert sev.options == {"S1": "30001", "S2": "30002"}


@pytest.mark.asyncio
@respx.mock
async def test_cloud_select_field_without_context_yields_empty_options():
    """Defensive: a select field with no contexts at all (edge case) is allowed."""
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "customfield_10502",
                    "name": "Empty Select",
                    "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
                },
            ],
        )
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10502/context").mock(
        return_value=httpx.Response(200, json=_paginated([]))
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert snap.custom_fields["customfield_10502"].options == {}


# ── TMP detection in project reflection ──────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_cloud_reflects_tmp_project_style():
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/project/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "10000",
                        "key": "PLAT",
                        "name": "Platform",
                        "projectTypeKey": "software",
                        "style": "classic",
                        "lead": {"accountId": "abc123"},
                    },
                    {
                        "id": "10010",
                        "key": "TMP1",
                        "name": "Team One",
                        "projectTypeKey": "software",
                        "style": "next-gen",
                        "lead": {"accountId": "def456"},
                    },
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert snap.projects["PLAT"].style == "classic"
    assert snap.projects["TMP1"].style == "next-gen"
    assert snap.projects["PLAT"].lead == "abc123"


# ── Cloud reads field-config items at /fields, not /items ────────────
@pytest.mark.asyncio
@respx.mock
async def test_cloud_reads_field_configuration_items_at_fields_endpoint():
    """Cloud's field-configuration items endpoint is documented as
    /rest/api/3/fieldconfiguration/{id}/fields (DC uses /items, which does
    not exist on Cloud — verified against the Cloud Platform OpenAPI). This
    test confirms the dialect override routes to the right segment."""
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/fieldconfiguration").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10000", "name": "Default Field Configuration", "description": ""},
                ]
            ),
        )
    )
    fields_route = respx.get(f"{CLOUD_ROOT}/fieldconfiguration/10000/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "summary", "isRequired": True, "isHidden": False},
                    {"id": "customfield_10042", "isRequired": False, "isHidden": False},
                ]
            ),
        )
    )
    # /items must NOT be called on Cloud.
    items_route = respx.get(f"{CLOUD_ROOT}/fieldconfiguration/10000/items").mock(
        return_value=httpx.Response(
            404,
            json={"errorMessages": ["No endpoint"], "errors": {}},
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    fc = snap.field_configurations["10000"]
    assert set(fc.items) == {"summary", "customfield_10042"}
    assert fc.items["summary"].required is True
    assert fields_route.called
    assert not items_route.called
