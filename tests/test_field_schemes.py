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
        if path == "/issuesecurityschemes":
            respx.get(f"{ROOT}{path}").mock(
                return_value=httpx.Response(status_code=200, json=_paginated_security_schemes([]))
            )
        else:
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


# ── Capability probe caching (H6) ────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_probe_transient_failure_is_not_cached():
    """A server error must not pin a tenant to v1 permanently (H6): the
    value is served for this call only, and the next call re-probes, so a
    genuine Field Schemes tenant recovers from a transient blip instead of
    being stranded on retired endpoints."""
    probe = respx.get(SCHEMES).mock(
        side_effect=[
            httpx.Response(500, text="oops"),
            httpx.Response(200, json=_paginated([])),
        ]
    )
    async with _engine() as eng:
        assert await eng.dialect.field_config_api() == "v1"
        assert probe.call_count == 1
        assert await eng.dialect.field_config_api() == "v2"
    # After the re-probe finds v2, it is cached: no third request.
    assert probe.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_probe_caches_v1_on_a_real_absence_answer():
    """404 is the server indicating the API is absent -- cache v1 (H6)."""
    probe = respx.get(SCHEMES).mock(return_value=httpx.Response(404, json={"errorMessages": ["not enabled"]}))
    async with _engine() as eng:
        assert await eng.dialect.field_config_api() == "v1"
        assert await eng.dialect.field_config_api() == "v1"  # served from cache
    assert probe.call_count == 1


# ── Retired-error retry on the remaining v1 write paths (M15) ────────
@pytest.mark.asyncio
@respx.mock
async def test_delete_field_configuration_retries_on_field_schemes_after_retired_400():
    """The create path got this recovery; every other classic write needs
    it too (M15) -- a bare TransportError mid-apply strands the run."""
    probe = respx.get(SCHEMES).mock(
        side_effect=[
            httpx.Response(404, json={"errorMessages": ["not enabled"]}),
            httpx.Response(200, json=_paginated([])),
        ]
    )
    respx.delete(f"{ROOT}/fieldconfiguration/9").mock(
        return_value=httpx.Response(
            400,
            json={"errorMessages": ["Cannot create a new field configuration. Please use Field Scheme instead."]},
        )
    )
    v2_delete = respx.delete(f"{SCHEMES}/9").mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        await eng.dialect.delete_field_configuration("9")
    assert v2_delete.called
    assert probe.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_update_field_configuration_retries_on_field_schemes_after_retired_400():
    probe = respx.get(SCHEMES).mock(
        side_effect=[
            httpx.Response(404, json={"errorMessages": ["not enabled"]}),
            httpx.Response(200, json=_paginated([])),
        ]
    )
    respx.put(f"{ROOT}/fieldconfiguration/9").mock(
        return_value=httpx.Response(
            400,
            json={"errorMessages": ["Cannot create a new field configuration. Please use Field Scheme instead."]},
        )
    )
    v2_update = respx.put(
        f"{SCHEMES}/9",
        json__eq={"name": "Renamed", "description": ""},
    ).mock(return_value=httpx.Response(204))
    async with _engine() as eng:
        await eng.dialect.update_field_configuration("9", name="Renamed", description="")
    assert v2_update.called
    assert probe.call_count == 2


# ── v2 removal body inspection (M14) ─────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_remove_fields_warns_on_unexpected_per_field_failure():
    _stub_probe(respx.mock, httpx.Response(200, json=_paginated([])))
    respx.delete(f"{SCHEMES}/fields").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"fieldId": "customfield_1", "errorMessages": [], "errors": {}},
                # "not associated" is the tolerated repeat-apply no-op.
                {"fieldId": "customfield_2", "errorMessages": ["field not associated"], "errors": {}},
                # Anything else would silently keep the desired state wrong.
                {
                    "fieldId": "customfield_3",
                    "errorMessages": [],
                    "errors": {"customfield_3": "field is locked by the scheme"},
                },
            ],
        )
    )
    async with _engine() as eng:
        with pytest.warns(UserWarning, match="customfield_3"):
            await eng.dialect.remove_fields_from_field_scheme(
                "7", field_ids=["customfield_1", "customfield_2", "customfield_3"]
            )
