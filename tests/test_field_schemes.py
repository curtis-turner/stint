"""Jira Cloud Field Schemes API (v2) routing and shapes.

Atlassian retired classic field configurations on Cloud in May 2026 and
replaced them with the Field Schemes API at /rest/api/3/config/fieldschemes.
These tests cover capability detection, the v2 write/read translations, and
the classic fallback for tenants that have not been migrated.
"""

import json
from typing import Any

import httpx
import pytest
import respx

from stint import APITokenAuth, create_engine

BASE = "https://acme.atlassian.net"
ROOT = f"{BASE}/rest/api/3"
SCHEMES = f"{ROOT}/config/fieldschemes"


def _engine():
    return create_engine(
        f"jira_cloud+{BASE}",
        auth=APITokenAuth(email="user@acme.com", token="tok"),
    )


def _paginated(values: list[Any]) -> dict[str, Any]:
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def _stub_probe(mock: respx.MockRouter, response: httpx.Response) -> None:
    mock.get(SCHEMES).mock(return_value=response)


# ── Capability detection ─────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_probe_selects_field_schemes_when_available():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    async with _engine() as eng:
        assert await eng.dialect.field_config_api() == "v2"
    # Cached: a second call must not re-probe.
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_probe_falls_back_to_classic_when_feature_disabled():
    _stub_probe(respx.mock, httpx.Response(404, json={"errorMessages": ["not enabled"]}))
    async with _engine() as eng:
        assert await eng.dialect.field_config_api() == "v1"


# ── Writes ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_field_configuration_routes_to_field_schemes():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    respx.get(f"{ROOT}/field").mock(return_value=httpx.Response(200, json=[{"id": "summary"}, {"id": "customfield_1"}]))
    respx.get(f"{ROOT}/field/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    create = respx.post(
        SCHEMES,
        json__eq={"name": "Bug FC", "description": "desc"},
    ).mock(return_value=httpx.Response(200, json={"id": 10000, "name": "Bug FC"}))
    associate = respx.put(
        f"{SCHEMES}/fields",
        json__eq={
            "customfield_1": [{"schemeIds": [10000]}],
            "summary": [{"schemeIds": [10000]}],
        },
    ).mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        new_id = await eng.dialect.create_field_configuration(name="Bug FC", description="desc")
    assert new_id == "10000"
    assert create.called
    # Classic configs show every field by default; v2 needs associations.
    assert associate.called


@pytest.mark.asyncio
@respx.mock
async def test_create_retries_on_field_schemes_after_retired_400():
    """A tenant migrated after the initial probe gets a retry on v2 instead
    of the retirement ConfigurationError."""
    probe = respx.get(SCHEMES).mock(
        side_effect=[
            httpx.Response(404, json={"errorMessages": ["not enabled"]}),
            httpx.Response(200, json=_paginated([])),
        ]
    )
    respx.post(f"{ROOT}/fieldconfiguration").mock(
        return_value=httpx.Response(
            400,
            json={
                "errorMessages": [
                    "Cannot create a new field configuration. Please use Field Scheme instead.",
                ],
                "errors": {},
            },
        )
    )
    create = respx.post(SCHEMES).mock(return_value=httpx.Response(200, json={"id": 10001}))
    respx.get(f"{ROOT}/field").mock(return_value=httpx.Response(200, json=[{"id": "summary"}]))
    respx.get(f"{ROOT}/field/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.put(f"{SCHEMES}/fields").mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        new_id = await eng.dialect.create_field_configuration(name="Bug FC")
    assert new_id == "10001"
    assert probe.call_count == 2
    assert create.called


@pytest.mark.asyncio
@respx.mock
async def test_v2_required_item_associates_field_and_sets_parameters():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    association = respx.put(
        f"{SCHEMES}/fields",
        json__eq={"customfield_10042": [{"schemeIds": [10000]}]},
    ).mock(return_value=httpx.Response(204))
    parameters = respx.put(
        f"{SCHEMES}/fields/parameters",
        json__eq={
            "customfield_10042": [
                {
                    "schemeIds": [10000],
                    "parameters": {"isRequired": True, "description": "triage"},
                }
            ]
        },
    ).mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        await eng.dialect.set_field_configuration_item(
            "10000",
            field_id="customfield_10042",
            required=True,
            description="triage",
        )
    assert association.called
    assert parameters.called


@pytest.mark.asyncio
@respx.mock
async def test_v2_hidden_item_removes_field_association():
    """v2 has no isHidden flag; hidden means 'not associated'."""
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    removal = respx.delete(
        f"{SCHEMES}/fields",
        json__eq={"customfield_10042": {"schemeIds": [10000]}},
    ).mock(return_value=httpx.Response(204))
    parameters = respx.put(f"{SCHEMES}/fields/parameters").mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        await eng.dialect.set_field_configuration_item(
            "10000",
            field_id="customfield_10042",
            hidden=True,
        )
    assert removal.called
    assert not parameters.called


@pytest.mark.asyncio
@respx.mock
async def test_v2_binds_project_to_field_scheme():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    bind = respx.put(
        f"{SCHEMES}/projects",
        json__eq={"10000": {"projectIds": [20000]}},
    ).mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        await eng.dialect.set_project_field_configuration_scheme(
            project_id="20000",
            scheme_id="10000",
        )
    assert bind.called


@pytest.mark.asyncio
@respx.mock
async def test_v2_field_association_chunks_at_api_limit():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    association = respx.put(f"{SCHEMES}/fields").mock(return_value=httpx.Response(204))
    field_ids = [f"customfield_{index}" for index in range(101)]
    async with _engine() as eng:
        await eng.dialect.associate_fields_with_field_scheme("10000", field_ids=field_ids)
    assert association.call_count == 2
    sizes = [len(json.loads(call.request.content)) for call in association.calls]
    assert sizes == [100, 1]


# ── v2 FCS → field-scheme merge ──────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_v2_fcs_mappings_merge_into_one_scheme_with_work_type_overrides():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    respx.get(f"{SCHEMES}/200/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"fieldId": "customfield_1", "parameters": {"isRequired": True}},
                ]
            ),
        )
    )
    respx.get(f"{SCHEMES}/201/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"fieldId": "customfield_1", "parameters": {"isRequired": False}},
                    {"fieldId": "customfield_2", "parameters": {"isRequired": False}},
                ]
            ),
        )
    )
    respx.get(f"{SCHEMES}/300/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"fieldId": "customfield_stale"},
                ]
            ),
        )
    )
    removal = respx.delete(
        f"{SCHEMES}/fields",
        json__eq={"customfield_stale": {"schemeIds": [300]}},
    ).mock(return_value=httpx.Response(204))
    association = respx.put(
        f"{SCHEMES}/fields",
        json__eq={
            "customfield_1": [{"schemeIds": [300]}],
            "customfield_2": [
                {"schemeIds": [300], "restrictedToWorkTypes": [42]},
            ],
        },
    ).mock(return_value=httpx.Response(204))
    parameters = respx.put(
        f"{SCHEMES}/fields/parameters",
        json__eq={
            "customfield_1": [
                {
                    "schemeIds": [300],
                    "parameters": {"isRequired": True},
                    "workTypeParameters": [{"workTypeId": 42, "isRequired": False}],
                }
            ],
            "customfield_2": [
                {
                    "schemeIds": [300],
                    "parameters": {"isRequired": False},
                    "workTypeParameters": [{"workTypeId": 42, "isRequired": False}],
                }
            ],
        },
    ).mock(return_value=httpx.Response(204))

    async with _engine() as eng:
        await eng.dialect.set_field_configuration_scheme_mappings(
            "300",
            mappings=[
                {"issueTypeId": "default", "fieldConfigurationId": "200"},
                {"issueTypeId": "42", "fieldConfigurationId": "201"},
            ],
        )
    assert removal.called
    assert association.called
    assert parameters.called


@pytest.mark.asyncio
@respx.mock
async def test_v2_fcs_merge_hides_default_field_from_excluded_work_type():
    """A field in the default configuration but omitted from one work type's
    configuration must be restricted to the *other* work types, not left
    visible everywhere."""
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    respx.get(f"{ROOT}/issuetype").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "42", "name": "Bug"}, {"id": "43", "name": "Task"}],
        )
    )
    respx.get(f"{SCHEMES}/200/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"fieldId": "customfield_1", "parameters": {"isRequired": True}},
                    {"fieldId": "customfield_2", "parameters": {"isRequired": True}},
                ]
            ),
        )
    )
    respx.get(f"{SCHEMES}/201/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated([{"fieldId": "customfield_1", "parameters": {"isRequired": False}}]),
        )
    )
    respx.get(f"{SCHEMES}/300/fields").mock(return_value=httpx.Response(200, json=_paginated([])))
    association = respx.put(
        f"{SCHEMES}/fields",
        json__eq={
            "customfield_1": [{"schemeIds": [300]}],
            "customfield_2": [
                {"schemeIds": [300], "restrictedToWorkTypes": [43]},
            ],
        },
    ).mock(return_value=httpx.Response(204))
    parameters = respx.put(
        f"{SCHEMES}/fields/parameters",
        json__eq={
            "customfield_1": [
                {
                    "schemeIds": [300],
                    "parameters": {"isRequired": True},
                    "workTypeParameters": [{"workTypeId": 42, "isRequired": False}],
                }
            ],
            "customfield_2": [
                {
                    "schemeIds": [300],
                    "parameters": {"isRequired": True},
                }
            ],
        },
    ).mock(return_value=httpx.Response(204))

    async with _engine() as eng:
        await eng.dialect.set_field_configuration_scheme_mappings(
            "300",
            mappings=[
                {"issueTypeId": "default", "fieldConfigurationId": "200"},
                {"issueTypeId": "42", "fieldConfigurationId": "201"},
            ],
        )
    assert association.called
    assert parameters.called


@pytest.mark.asyncio
@respx.mock
async def test_update_field_configuration_routes_to_field_schemes():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    update = respx.put(
        f"{SCHEMES}/10000",
        json__eq={"name": "Renamed", "description": "new"},
    ).mock(return_value=httpx.Response(200, json={}))
    async with _engine() as eng:
        await eng.dialect.update_field_configuration("10000", name="Renamed", description="new")
    assert update.called


@pytest.mark.asyncio
@respx.mock
async def test_update_field_configuration_stays_classic_on_v1():
    _stub_probe(respx.mock, httpx.Response(404, json={"errorMessages": ["not enabled"]}))
    update = respx.put(
        f"{ROOT}/fieldconfiguration/10000",
        json__eq={"name": "Renamed"},
    ).mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        await eng.dialect.update_field_configuration("10000", name="Renamed")
    assert update.called


# ── Reads ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_uses_v2_schemes_and_project_bindings():
    respx.get(f"{ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={"baseUrl": BASE, "version": "1001.0.0", "deploymentType": "Cloud"},
        )
    )
    respx.get(f"{ROOT}/field/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{ROOT}/project/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "20000",
                        "key": "PLAT",
                        "name": "Platform",
                        "projectTypeKey": "software",
                        "style": "classic",
                    }
                ]
            ),
        )
    )
    for path in (
        "/issuetypescheme/project",
        "/issuetypescreenscheme/project",
        "/screens",
        "/screenscheme",
        "/issuetypescheme",
        "/issuetypescheme/mapping",
        "/issuetypescreenscheme",
        "/issuetypescreenscheme/mapping",
        "/issuesecurityschemes",
    ):
        respx.get(f"{ROOT}{path}").mock(return_value=httpx.Response(200, json=_paginated([])))

    _stub_probe(
        respx.mock,
        httpx.Response(
            200,
            json=_paginated([{"id": 10000, "name": "Platform Field Scheme", "description": ""}]),
        ),
    )
    respx.get(f"{SCHEMES}/projects").mock(
        return_value=httpx.Response(
            200,
            json=_paginated([{"projectId": 20000, "schemeId": 10000}]),
        )
    )
    respx.get(f"{SCHEMES}/10000/fields").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "fieldId": "customfield_10042",
                        "parameters": {"isRequired": True},
                    }
                ]
            ),
        )
    )

    async with _engine() as eng:
        snap = await eng.reflect()

    assert snap.projects["PLAT"].field_configuration_scheme_id == "10000"
    assert snap.field_configurations["10000"].name == "Platform Field Scheme"
    assert snap.field_configurations["10000"].items["customfield_10042"].required is True
    assert snap.field_configuration_schemes["10000"].name == "Platform Field Scheme"
    # Classic endpoints must not be touched on a v2 tenant.
    assert not any("fieldconfiguration" in str(call.request.url) for call in respx.calls)
