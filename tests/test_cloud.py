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


def _paginated_security_schemes(values: list[Any]) -> dict[str, Any]:
    """Single-page response shape used by Get Issues Security Schemes endpoint.

    The endpoint carries pagination metadata even though this shape does not
    use standard "values" pagination -- without `isLast` a non-empty page
    would loop forever in `paginate` (the bug, incidentally, that fixed the
    B2-era mocked shape)."""
    return {
        "issueSecuritySchemes": values,
        "maxResults": 50,
        "startAt": 0,
        "total": len(values),
        "isLast": True,
    }


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
    mock.get(f"{CLOUD_ROOT}/field/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{CLOUD_ROOT}/project/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/screens").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/screenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescheme/mapping").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescreenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetypescreenscheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/fieldconfiguration").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/fieldconfigurationscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/fieldconfigurationscheme/project").mock(
        return_value=httpx.Response(200, json=_paginated([]))
    )
    mock.get(f"{CLOUD_ROOT}/issuesecurityschemes").mock(
        return_value=httpx.Response(200, json=_paginated_security_schemes([]))
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
    respx.get(f"{CLOUD_ROOT}/field/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "customfield_10501",
                        "name": "Severity",
                        "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
                    },
                ]
            ),
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
    respx.get(f"{CLOUD_ROOT}/field/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "customfield_10502",
                        "name": "Empty Select",
                        "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
                    },
                ]
            ),
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
    /rest/api/3/fieldconfiguration/{id}/fields (verified against the Cloud
    Platform OpenAPI). This test confirms the dialect routes to that
    segment."""
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


# ── Bulk projectId filter chunking (M10) ─────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_project_scheme_bindings_chunk_project_ids():
    """A tenant with more projects than one request's projectId cap must
    still reflect: ids are chunked (batch 50) and results merged, instead
    of one oversized request that 400s or silently truncates (M10)."""
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/config/fieldschemes").mock(
        return_value=httpx.Response(404, json={"errorMessages": ["not enabled"]})
    )
    projects = [{"id": str(100 + i), "key": f"P{i}", "name": f"Project {i}"} for i in range(55)]
    respx.get(f"{CLOUD_ROOT}/project/search").mock(return_value=httpx.Response(200, json=_paginated(projects)))
    # last page startAt extends; simple full-page single response is fine
    its_route = respx.get(f"{CLOUD_ROOT}/issuetypescheme/project").mock(
        return_value=httpx.Response(200, json=_paginated([{"issueTypeScheme": {"id": "900"}, "projectIds": ["100"]}]))
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    # 55 ids / batch 50 → 2 requests per binding endpoint.
    assert its_route.call_count == 2
    assert snap.projects["P0"].issuetype_scheme_id == "900"


@pytest.mark.asyncio
@respx.mock
async def test_reflect_sends_each_chunk_with_its_own_ids():
    """Guard the actual failure mode: a single request carrying all ids
    would 400. Assert the per-request ids stay within the batch size."""
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/config/fieldschemes").mock(
        return_value=httpx.Response(404, json={"errorMessages": ["not enabled"]})
    )
    projects = [{"id": str(100 + i), "key": f"P{i}", "name": f"Project {i}"} for i in range(55)]
    respx.get(f"{CLOUD_ROOT}/project/search").mock(return_value=httpx.Response(200, json=_paginated(projects)))
    route = respx.get(f"{CLOUD_ROOT}/issuetypescheme/project").mock(
        return_value=httpx.Response(200, json=_paginated([]))
    )
    async with _cloud_engine() as eng:
        await eng.reflect()
    id_counts = []
    for call in route.calls:
        request_url = str(call.request.url)
        id_counts.append(request_url.count("projectId="))
    assert len(id_counts) == 2
    assert id_counts[0] == 50 and id_counts[1] == 5


# ── Issue security schemes: levels actually reflected (H8, M16) ──────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_issue_security_schemes_embeds_levels_and_members():
    """The list endpoint never carries levels; without the per-scheme GET
    every reflect produced levels=[] and declared levels diffed as eternal
    phantom drift (H8)."""
    _stub_empty(respx.mock)
    respx.get(f"{CLOUD_ROOT}/issuesecurityschemes").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_security_schemes(
                [{"id": 10000, "name": "Sec", "description": "", "defaultSecurityLevelId": 10021}]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuesecurityschemes/10000").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 10000,
                "levels": [
                    {
                        "id": 10021,
                        "name": "Admins",
                        "description": "Only admins see it",
                        "isDefault": True,
                        "members": [{"id": 1, "holder": {"type": "user", "name": "acct-1"}}],
                    },
                    {
                        "id": 10022,
                        "name": "Groups",
                        "description": "",
                        "isDefault": False,
                        # No inline members: the level-member endpoint fills in.
                    },
                ],
            },
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuesecurityschemes/level/member").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": [
                    {"id": 2, "holder": {"type": "group", "name": "admins"}},
                    {"id": 3, "holder": {"type": "user", "name": "acct-2"}},
                ]
            },
        )
    )
    async with _cloud_engine() as eng:
        snap = await eng.reflect()
    scheme = snap.security_schemes["10000"]
    assert [lv.id for lv in scheme.levels] == ["10021", "10022"]
    assert scheme.levels[0].members[0].type == "user"
    assert scheme.levels[1].members[0].type == "group"
    assert scheme.default_security_level_id == "10021"


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_security_scheme_raises_on_missing_id():
    """Empty 2xx body → _expect_id's ReflectionError, not an AttributeError,
    and not a silently-untracked created scheme (M16)."""
    from stint.exceptions import ReflectionError

    respx.post(f"{CLOUD_ROOT}/issuesecurityschemes").mock(return_value=httpx.Response(200, json={}))
    async with _cloud_engine() as eng:
        with pytest.raises(ReflectionError):
            await eng.dialect.create_issue_security_scheme(name="S")


# ── Work-type id cache invalidation (M11) ────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_and_delete_issuetype_invalidate_work_type_cache():
    """Without the reset, "visible everywhere except X" restrictions are
    built from a stale set that silently excludes a freshly created
    issuetype (M11)."""
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(
        side_effect=[
            httpx.Response(200, json=[{"id": "1"}]),
            httpx.Response(200, json=[{"id": "1"}, {"id": "2"}]),
            httpx.Response(200, json=[{"id": "1"}]),
        ]
    )
    respx.post(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(201, json={"id": "2"}))
    respx.delete(f"{CLOUD_ROOT}/issuetype/2").mock(return_value=httpx.Response(204))
    async with _cloud_engine() as eng:
        assert await eng.dialect._all_work_type_ids() == frozenset({"1"})
        await eng.dialect.create_issuetype(name="New")
        # The cache was reset after the create: the fresh id shows up.
        assert await eng.dialect._all_work_type_ids() == frozenset({"1", "2"})
        await eng.dialect.delete_issuetype("2")
        assert await eng.dialect._all_work_type_ids() == frozenset({"1"})
