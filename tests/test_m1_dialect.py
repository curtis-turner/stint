"""Jira DC dialect: detect() against /serverInfo, reflect() of all admin objects.

All HTTP is mocked via respx; no real network involved.
"""

from typing import Any

import httpx
import pytest
import respx

from pensum import (
    AuthenticationError,
    PATAuth,
    PermissionError,
    create_engine,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


def _cloud_engine():
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("test-token"))


def _paginated(values: list[Any]) -> dict[str, Any]:
    """Single-page response shape used by Jira admin endpoints."""
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def _stub_empty_admin(mock: respx.MockRouter, root: str = CLOUD_ROOT) -> None:
    """Mock every admin endpoint with an empty response.

    Lets a test stub only the endpoints it cares about. The default is "nothing
    exists on the server" for everything else.
    """
    mock.get(f"{root}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "1001.0.0",
                "deploymentType": "Cloud",
            },
        )
    )
    mock.get(f"{root}/field").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{root}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{root}/project/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/screens").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/screenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/issuetypescheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/issuetypescheme/mapping").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/issuetypescheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/issuetypescreenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/issuetypescreenscheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/fieldconfiguration").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/fieldconfigurationscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{root}/fieldconfigurationscheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))


# ── reflect(): custom fields ──────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_server_info():
    _stub_empty_admin(respx.mock)
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert snap.server_info.deployment_type == "Cloud"
    assert snap.server_info.version == "1001.0.0"


@pytest.mark.asyncio
@respx.mock
async def test_reflect_filters_out_system_fields():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "summary", "name": "Summary"},
                {"id": "description", "name": "Description"},
                {"id": "assignee", "name": "Assignee"},
            ],
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert snap.custom_fields == {}


@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_custom_fields_with_options():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "summary", "name": "Summary"},
                {
                    "id": "customfield_10042",
                    "name": "Severity",
                    "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
                },
                {
                    "id": "customfield_10043",
                    "name": "Root Cause",
                    "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"},
                },
            ],
        )
    )
    # Select-field options are fetched per field through the default context.
    # Text field is skipped.
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(200, json=_paginated([{"id": "ctx-1"}]))
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10100", "value": "S1"},
                    {"id": "10101", "value": "S2"},
                    {"id": "10102", "value": "S3"},
                    {"id": "10103", "value": "S4"},
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    sev = snap.custom_fields["customfield_10042"]
    assert sev.options == {"S1": "10100", "S2": "10101", "S3": "10102", "S4": "10103"}
    rc = snap.custom_fields["customfield_10043"]
    assert rc.options == {}  # text field, no option fetch


# ── reflect(): issuetypes ────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_issuetypes():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "10001", "name": "Bug", "description": "Defect", "subtask": False},
                {"id": "10002", "name": "Story", "description": "", "subtask": False},
                {"id": "10003", "name": "Sub-task", "description": "", "subtask": True},
            ],
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert set(snap.issuetypes) == {"10001", "10002", "10003"}
    assert snap.issuetypes["10001"].name == "Bug"
    assert snap.issuetypes["10003"].subtask is True


# ── reflect(): projects ──────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_projects():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/project/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "10000",
                        "key": "PLAT",
                        "name": "Platform",
                        "lead": {"name": "cturner"},
                        "projectTypeKey": "software",
                    },
                    {
                        "id": "10010",
                        "key": "DOCS",
                        "name": "Documentation",
                        "projectTypeKey": "business",
                    },
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert set(snap.projects) == {"PLAT", "DOCS"}
    plat = snap.projects["PLAT"]
    assert plat.lead == "cturner"
    assert plat.project_type_key == "software"


@pytest.mark.asyncio
@respx.mock
async def test_reflect_populates_project_scheme_bindings():
    """Reflect must look up each project's current IssueTypeScheme,
    IssueTypeScreenScheme, and FieldConfigurationScheme so the diff can
    detect drift against existing projects (issue #5)."""
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/project/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10000", "key": "PLAT", "name": "Platform", "projectTypeKey": "software"},
                    {"id": "10010", "key": "DOCS", "name": "Documentation", "projectTypeKey": "business"},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/project").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"issueTypeScheme": {"id": "20000", "name": "Default"}, "projectIds": ["10000", "10010"]},
                    {"issueTypeScheme": {"id": "20001", "name": "Platform ITS"}, "projectIds": ["10000"]},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetypescreenscheme/project").mock(
        return_value=httpx.Response(
            200,
            json=_paginated([{"issueTypeScreenScheme": {"id": "30000"}, "projectIds": ["10000"]}]),
        )
    )
    respx.get(f"{CLOUD_ROOT}/fieldconfigurationscheme/project").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"projectIds": ["10010"]},  # no scheme key → uses default, ignored
                    {"fieldConfigurationScheme": {"id": "40000"}, "projectIds": ["10000"]},
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    plat = snap.projects["PLAT"]
    docs = snap.projects["DOCS"]
    # Last writer wins when a project shows up in two scheme entries; here
    # PLAT is in both default and Platform ITS, so the second one wins.
    assert plat.issuetype_scheme_id == "20001"
    assert plat.issuetype_screen_scheme_id == "30000"
    assert plat.field_configuration_scheme_id == "40000"
    # DOCS is bound to the default IssueTypeScheme but has no explicit
    # ITSS / FCS binding → both stay None.
    assert docs.issuetype_scheme_id == "20000"
    assert docs.issuetype_screen_scheme_id is None
    assert docs.field_configuration_scheme_id is None


# ── reflect(): screens, screen schemes, ITSS ─────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_screens_with_tabs_and_fields():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/screens").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10100", "name": "Bug Create Screen", "description": ""},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/screens/10100/tabs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "11000", "name": "Field Tab"},
            ],
        )
    )
    respx.get(f"{CLOUD_ROOT}/screens/10100/tabs/11000/fields").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "summary", "name": "Summary"},
                {"id": "customfield_10042", "name": "Severity"},
            ],
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    screen = snap.screens["10100"]
    assert screen.name == "Bug Create Screen"
    assert len(screen.tabs) == 1
    assert screen.tabs[0].fields == ("summary", "customfield_10042")


@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_screen_schemes_and_itss():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/screenscheme").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "10200",
                        "name": "Bug Screen Scheme",
                        "screens": {"default": "10100", "create": "10100", "edit": "10100"},
                    },
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetypescreenscheme").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10300", "name": "Bug ITSS", "description": ""},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetypescreenscheme/mapping").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"issueTypeId": "default", "screenSchemeId": "10200"},
                    {"issueTypeId": "10001", "screenSchemeId": "10200"},
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    ss = snap.screen_schemes["10200"]
    assert ss.mappings["default"] == "10100"
    itss = snap.issuetype_screen_schemes["10300"]
    assert len(itss.mappings) == 2
    assert {m.issuetype_id for m in itss.mappings} == {"default", "10001"}


# ── reflect(): field configurations + schemes ───────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_field_configurations_with_items():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/fieldconfiguration").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10400", "name": "Bug Field Configuration", "description": ""},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/fieldconfiguration/10400/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "summary", "isRequired": True, "isHidden": False, "description": ""},
                    {"id": "customfield_10042", "isRequired": True, "isHidden": False, "description": ""},
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    fc = snap.field_configurations["10400"]
    assert set(fc.items) == {"summary", "customfield_10042"}
    assert fc.items["summary"].required is True


@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_field_configuration_schemes():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/fieldconfigurationscheme").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "10500", "name": "Bug FCS", "description": ""},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/fieldconfigurationscheme/mapping").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"issueTypeId": "default", "fieldConfigurationId": "10400"},
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    fcs = snap.field_configuration_schemes["10500"]
    assert fcs.name == "Bug FCS"
    assert len(fcs.mappings) == 1
    assert fcs.mappings[0].field_configuration_id == "10400"


# ── HTTP error mapping ────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_401_raises_authentication_error():
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(401, json={"errorMessages": ["Unauthorized"]})
    )
    async with _cloud_engine() as eng:
        with pytest.raises(AuthenticationError):
            await eng.detect()


@pytest.mark.asyncio
@respx.mock
async def test_403_raises_permission_error():
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(403, json={"errorMessages": ["Site Admin required"]})
    )
    async with _cloud_engine() as eng:
        with pytest.raises(PermissionError) as e:
            await eng.reflect()
    assert "Admin" in str(e.value)


# ── pagination ───────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_handles_multi_page_results():
    """Two-page response: first has isLast=False, second has isLast=True."""
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/screens").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "values": [{"id": "1", "name": "S1", "description": ""}],
                    "isLast": False,
                    "startAt": 0,
                    "maxResults": 1,
                },
            ),
            httpx.Response(
                200,
                json={
                    "values": [{"id": "2", "name": "S2", "description": ""}],
                    "isLast": True,
                    "startAt": 1,
                    "maxResults": 1,
                },
            ),
        ]
    )
    respx.get(f"{CLOUD_ROOT}/screens/1/tabs").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{CLOUD_ROOT}/screens/2/tabs").mock(return_value=httpx.Response(200, json=[]))
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    assert set(snap.screens) == {"1", "2"}


@pytest.mark.asyncio
@respx.mock
async def test_reflect_skips_screens_whose_tabs_endpoint_returns_400():
    """Cloud tenants with team-managed projects expose synthetic screens in
    /screens whose /tabs endpoint returns 400. Real example from
    cumulusec.atlassian.net: screen 10000 ("EMAL: Simple Default Issue
    Screen") is listed but its tabs lookup returns
    {"errorMessages":["Screen with id 10000 does not exist"]}. The reflect
    must not crash; the bad screen is skipped with a warning."""
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/screens").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "1", "name": "Default Screen", "description": ""},
                    {"id": "10000", "name": "EMAL: Simple Default Issue Screen", "description": ""},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/screens/1/tabs").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{CLOUD_ROOT}/screens/10000/tabs").mock(
        return_value=httpx.Response(400, json={"errorMessages": ["Screen with id 10000 does not exist"], "errors": {}})
    )
    with pytest.warns(UserWarning, match="skipping screen 10000"):
        async with _cloud_engine() as eng:
            snap = await eng.reflect()
    assert set(snap.screens) == {"1"}


# ── reflect(): issuetype schemes ─────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_captures_issuetype_schemes_with_members():
    """IssueTypeScheme reflect: members come from /issuetypescheme/mapping
    filtered to the scheme id. Members are kept ordered."""
    _stub_empty_admin(respx.mock)
    respx.get(f"{CLOUD_ROOT}/issuetypescheme").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "10200",
                        "name": "PLAT Issue Type Scheme",
                        "description": "Auto-derived",
                        "defaultIssueTypeId": "10100",
                        "isDefault": False,
                    },
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/mapping").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"issueTypeSchemeId": "10200", "issueTypeId": "10100"},
                    {"issueTypeSchemeId": "10200", "issueTypeId": "10101"},
                    {"issueTypeSchemeId": "99999", "issueTypeId": "20000"},  # other scheme, ignored
                ]
            ),
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    its = snap.issuetype_schemes["10200"]
    assert its.name == "PLAT Issue Type Scheme"
    assert its.default_issuetype_id == "10100"
    assert its.issuetype_ids == ("10100", "10101")
