"""TMP dialect build phases 2-4: fields gateway, field CRUD, reads, work
types, the layout write, and reflect.

All HTTP is mocked via respx against the exact shapes captured from a live
tenant and confirmed working over token auth 2026-07-05 (see
tmp_internal_endpoints.md). TmpDialect is not registered as a selectable
dialect yet (it doesn't satisfy BaseDialect until later build phases), so
these tests construct it directly rather than through create_engine.
"""

import json

import httpx
import pytest
import respx

from stint.client.auth import APITokenAuth
from stint.client.http import JiraHTTPClient
from stint.dialects.jira.tmp import TmpDialect
from stint.dialects.jira.tmp.dialect import _gql_enum, _gql_options, _gql_or_null, _gql_str
from stint.dialects.jira.tmp.graphql import FIELDS_GATEWAY_PATH, LAYOUT_READ_PATH, GraphQLClient
from stint.dialects.jira.tmp.internal_rest import ISSUE_LAYOUTS_PATH, SIMPLIFIED_PATH
from stint.dialects.jira.tmp.models import TmpFieldOption, TmpLayout, TmpLayoutItem, TmpLayoutOwner
from stint.exceptions import TmpApiError

BASE = "https://cumulusec.atlassian.net"
GATEWAY_URL = f"{BASE}{FIELDS_GATEWAY_PATH}"
GIRA_URL = f"{BASE}{LAYOUT_READ_PATH}"
SIMPLIFIED_URL = f"{BASE}{SIMPLIFIED_PATH}"
ISSUE_LAYOUTS_URL = f"{BASE}{ISSUE_LAYOUTS_PATH}"
API_ROOT = f"{BASE}/rest/api/3"
TENANT_INFO_URL = f"{BASE}/_edge/tenant_info"
ISSUETYPE_PROJECT_URL = f"{API_ROOT}/issuetype/project"


def _client() -> JiraHTTPClient:
    return JiraHTTPClient(BASE, auth=APITokenAuth(email="you@example.com", token="tok"))


def _dialect() -> TmpDialect:
    return TmpDialect(_client())


# ── Query-building helpers ────────────────────────────────────────────
def test_gql_str_escapes_quotes_and_backslashes():
    assert _gql_str('say "hi"') == '"say \\"hi\\""'
    assert _gql_str("back\\slash") == '"back\\\\slash"'


def test_gql_or_null():
    assert _gql_or_null(None) == "null"
    assert _gql_or_null("10094") == '"10094"'


def test_gql_enum_accepts_valid_literal():
    assert _gql_enum("ORANGE_DARKER") == "ORANGE_DARKER"


def test_gql_enum_rejects_non_identifier():
    with pytest.raises(TmpApiError, match="not a valid GraphQL enum literal"):
        _gql_enum('ORANGE_DARKER") @maliciousDirective(x: "')


def test_gql_options_existing_option_has_no_external_uuid_or_color():
    rendered = _gql_options([TmpFieldOption(value="test1", option_id="10094")])
    assert rendered == '[{value: "test1", optionId: "10094"}]'


def test_gql_options_new_option_gets_external_uuid_and_bare_color_enum():
    rendered = _gql_options(
        [TmpFieldOption(value="test2", option_id=None, external_uuid="9497b3eb-...", color="ORANGE_DARKER")]
    )
    assert "optionId: null" in rendered
    assert 'externalUuid: "9497b3eb-..."' in rendered
    assert "color: ORANGE_DARKER" in rendered
    assert 'color: "ORANGE_DARKER"' not in rendered  # regression: must be a bare enum, not a string


def test_gql_options_new_option_without_external_uuid_generates_one():
    rendered = _gql_options([TmpFieldOption(value="test2", option_id=None)])
    assert "externalUuid:" in rendered


# ── GraphQLClient ──────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_graphql_client_sends_operation_name_and_returns_data():
    route = respx.post(GATEWAY_URL).mock(return_value=httpx.Response(200, json={"data": {"ok": True}}))
    client = GraphQLClient(_client())
    data = await client.query(FIELDS_GATEWAY_PATH, query="query Foo { x }", operation_name="Foo")
    assert data == {"ok": True}
    body = json.loads(route.calls.last.request.content)
    assert body["operationName"] == "Foo"
    assert body["query"] == "query Foo { x }"


@pytest.mark.asyncio
@respx.mock
async def test_graphql_client_raises_on_errors_array():
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "message": "You MUST provide a 'X-ExperimentalApi : JiraProject' HTTP header",
                        "extensions": {"classification": "BetaHeaderOptInException"},
                    }
                ]
            },
        )
    )
    client = GraphQLClient(_client())
    with pytest.raises(TmpApiError, match="BetaHeaderOptInException"):
        await client.query(FIELDS_GATEWAY_PATH, query="query Foo { x }", operation_name="Foo")


@pytest.mark.asyncio
@respx.mock
async def test_graphql_client_forwards_extra_headers():
    route = respx.post(GATEWAY_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = GraphQLClient(_client())
    await client.query(
        FIELDS_GATEWAY_PATH,
        query="query Foo { x }",
        operation_name="Foo",
        extra_headers={"X-ExperimentalApi": "JiraProject"},
    )
    assert route.calls.last.request.headers["X-ExperimentalApi"] == "JiraProject"


# ── create_field / edit_field / delete_field ───────────────────────────
def _field_association(field_id: str, name: str, description: str, scope: str, options: list[dict]) -> dict:
    return {
        "field": {"fieldId": field_id, "name": name, "description": description, "scope": scope},
        "fieldOptions": {"edges": [{"node": o} for o in options]},
    }


@pytest.mark.asyncio
@respx.mock
async def test_create_field_parses_result_and_sends_optin_directive():
    route = respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "jira": {
                        "createCustomFieldInProjectAndAddToAllIssueTypes": {
                            "success": True,
                            "fieldAssociationWithIssueTypes": _field_association(
                                "customfield_10179",
                                "test-options",
                                "",
                                "PROJECT",
                                [{"optionId": 10094, "value": "test1", "color": None}],
                            ),
                        }
                    }
                }
            },
        )
    )
    dialect = _dialect()
    field = await dialect.create_field(
        cloud_id="db4af41e-c12f-4cf7-8619-87223f09d63f",
        project_id="10001",
        type_key="com.atlassian.jira.plugin.system.customfieldtypes:select",
        name="test-options",
        options=[TmpFieldOption(value="test1", option_id=None, external_uuid="79bdb5d3-...")],
    )
    assert field.field_id == "customfield_10179"
    assert field.scope == "PROJECT"
    assert field.options == (TmpFieldOption(value="test1", option_id="10094", color=None),)
    body = json.loads(route.calls.last.request.content)
    assert body["operationName"] == "StintTmpFieldCreate"
    assert "JiraProjectFieldsPageCreateCustomField" in body["query"]


@pytest.mark.asyncio
@respx.mock
async def test_create_field_raises_when_success_false():
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "jira": {
                        "createCustomFieldInProjectAndAddToAllIssueTypes": {
                            "success": False,
                            "fieldAssociationWithIssueTypes": None,
                        }
                    }
                }
            },
        )
    )
    with pytest.raises(TmpApiError, match="success != true"):
        await _dialect().create_field(cloud_id="cid", project_id="10001", type_key="...:textfield", name="x")


@pytest.mark.asyncio
@respx.mock
async def test_edit_field_full_replacement_options():
    route = respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "jira": {
                        "editCustomField": {
                            "success": True,
                            "fieldAssociationWithIssueTypes": _field_association(
                                "customfield_10179",
                                "test-options-1",
                                "test options descriptions",
                                "PROJECT",
                                [
                                    {"optionId": 10094, "value": "test1", "color": None},
                                    {
                                        "optionId": 10095,
                                        "value": "test2",
                                        "color": {"colorKey": "ORANGE_DARKER"},
                                    },
                                ],
                            ),
                        }
                    }
                }
            },
        )
    )
    field = await _dialect().edit_field(
        cloud_id="cid",
        project_id="10001",
        field_id="customfield_10179",
        name="test-options-1",
        description="test options descriptions",
        options=[
            TmpFieldOption(value="test1", option_id="10094"),
            TmpFieldOption(value="test2", option_id=None, external_uuid="9497b3eb-...", color="ORANGE_DARKER"),
        ],
    )
    assert field.name == "test-options-1"
    assert field.options[1].option_id == "10095"
    assert field.options[1].color == "ORANGE_DARKER"
    body = json.loads(route.calls.last.request.content)
    assert "JiraProjectFieldsPageEditCustomField" in body["query"]
    assert "color: ORANGE_DARKER" in body["query"]


@pytest.mark.asyncio
@respx.mock
async def test_delete_field_success():
    route = respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(200, json={"data": {"jira": {"deleteCustomField": {"success": True}}}})
    )
    await _dialect().delete_field(cloud_id="cid", project_id="10001", field_id="customfield_10179")
    body = json.loads(route.calls.last.request.content)
    assert "JiraProjectFieldsPageDeleteCustomField" in body["query"]
    assert "customfield_10179" in body["query"]


@pytest.mark.asyncio
@respx.mock
async def test_delete_field_raises_when_success_false():
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(200, json={"data": {"jira": {"deleteCustomField": {"success": False}}}})
    )
    with pytest.raises(TmpApiError, match="success != true"):
        await _dialect().delete_field(cloud_id="cid", project_id="10001", field_id="customfield_10179")


def _field_node(field_id: str, name: str, scope: str, type_key: str, options: list[dict] | None = None) -> dict:
    return {
        "node": {
            "field": {"fieldId": field_id, "name": name, "scope": scope, "typeKey": type_key},
            "fieldOptions": {"edges": [{"node": o} for o in (options or [])]},
        }
    }


def _enumeration_response(edges: list[dict], *, has_next_page: bool = False) -> dict:
    return {
        "data": {
            "jira": {
                "jiraProjectByKey": {
                    "projectWithVisibleIssueTypeIds": {
                        "fieldAssociationWithIssueTypes": {
                            "edges": edges,
                            "pageInfo": {"hasNextPage": has_next_page},
                        }
                    }
                }
            }
        }
    }


# ── enumerate_fields ────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_enumerate_fields_parses_edges_and_sends_experimental_header():
    select_type = "com.atlassian.jira.plugin.system.customfieldtypes:select"
    team_type = "com.atlassian.jira.plugin.system.customfieldtypes:atlassian-team"
    route = respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_enumeration_response(
                [
                    _field_node("customfield_10179", "test-options", "PROJECT", select_type),
                    _field_node("customfield_10001", "Team", "GLOBAL", team_type),
                ]
            ),
        )
    )
    fields = await _dialect().enumerate_fields(cloud_id="cid", project_key="VM")
    assert [f.field_id for f in fields] == ["customfield_10179", "customfield_10001"]
    assert fields[0].scope == "PROJECT"
    assert fields[1].scope == "GLOBAL"
    assert route.calls.last.request.headers["X-ExperimentalApi"] == "JiraProject"


@pytest.mark.asyncio
@respx.mock
async def test_enumerate_fields_parses_options():
    select_type = "com.atlassian.jira.plugin.system.customfieldtypes:select"
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_enumeration_response(
                [
                    _field_node(
                        "customfield_10179",
                        "test-options",
                        "PROJECT",
                        select_type,
                        options=[{"optionId": 10094, "value": "test1", "color": None}],
                    )
                ]
            ),
        )
    )
    fields = await _dialect().enumerate_fields(cloud_id="cid", project_key="VM")
    assert fields[0].options == (TmpFieldOption(value="test1", option_id="10094", color=None),)


@pytest.mark.asyncio
@respx.mock
async def test_enumerate_fields_raises_loud_on_truncation():
    respx.post(GATEWAY_URL).mock(return_value=httpx.Response(200, json=_enumeration_response([], has_next_page=True)))
    with pytest.raises(TmpApiError, match="cursor pagination is not implemented"):
        await _dialect().enumerate_fields(cloud_id="cid", project_key="VM", page_size=1)


# ── read_layout ──────────────────────────────────────────────────────────
def _layout_response() -> dict:
    """Shaped after the live-confirmed 2026-07-05 capture (Task work type, VM/10001)."""
    return {
        "data": {
            "issueLayoutConfiguration": {
                "__typename": "JiraIssueLayoutConfigurationResult",
                "issueLayoutResult": {
                    "id": "c6b31d04-d184-48ef-877e-a11908f07576",
                    "name": "KAN-Task",
                    "usageInfo": {
                        "edges": [
                            {
                                "currentProject": True,
                                "node": {
                                    "layoutOwners": [
                                        {
                                            "__typename": "JiraIssueLayoutIssueTypeOwner",
                                            "id": "10007",
                                            "name": "Task",
                                            "description": "Tasks track small, distinct pieces of work.",
                                            "avatarId": "10318",
                                            "iconUrl": "https://cumulusec.atlassian.net/.../10318",
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                    "containers": [
                        {
                            "containerType": "CONTENT",
                            "items": {
                                "nodes": [
                                    {
                                        "__typename": "JiraIssueItemFieldItem",
                                        "fieldItemId": "summary",
                                        "containerPosition": 100,
                                    }
                                ]
                            },
                        },
                        {
                            "containerType": "PRIMARY",
                            "items": {
                                "nodes": [
                                    {
                                        "__typename": "JiraIssueItemFieldItem",
                                        "fieldItemId": "assignee",
                                        "containerPosition": 100,
                                    }
                                ]
                            },
                        },
                    ],
                },
                "metadata": {
                    "configuration": {
                        "items": {
                            "nodes": [
                                {
                                    "__typename": "JiraIssueLayoutFieldItemConfiguration",
                                    "fieldItemId": "summary",
                                    "key": "summary",
                                    "name": "Summary",
                                    "type": "summary",
                                    "custom": False,
                                    "global": True,
                                    "required": True,
                                    "externalUuid": "1ddf370e-...",
                                    "description": "",
                                    "operations": {"editable": False, "removable": False},
                                    "provider": {"key": "jira-platform", "name": "Jira"},
                                },
                                {
                                    "__typename": "JiraIssueLayoutFieldItemConfiguration",
                                    "fieldItemId": "assignee",
                                    "key": "assignee",
                                    "name": "Assignee",
                                    "type": "assignee",
                                    "custom": False,
                                    "global": True,
                                    "required": False,
                                    "externalUuid": "76e94278-...",
                                    "description": "",
                                    "operations": {"editable": False, "removable": True},
                                    "provider": {"key": "jira-platform", "name": "Jira"},
                                },
                            ]
                        }
                    }
                },
            }
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_read_layout_parses_owner_and_items():
    respx.post(GIRA_URL).mock(return_value=httpx.Response(200, json=_layout_response()))
    layout = await _dialect().read_layout(project_id="10001", issuetype_id=10007)
    assert layout.layout_id == "c6b31d04-d184-48ef-877e-a11908f07576"
    assert layout.owner.id == "10007"
    assert layout.owner.name == "Task"
    assert [i.field_id for i in layout.items] == ["summary", "assignee"]
    assert layout.items[0].section == "content"
    assert layout.items[1].section == "primary"
    assert layout.items[0].required is True
    assert layout.items[1].global_ is True


@pytest.mark.asyncio
@respx.mock
async def test_read_layout_raises_on_unexpected_typename():
    resp = _layout_response()
    resp["data"]["issueLayoutConfiguration"]["__typename"] = "SomethingElse"
    respx.post(GIRA_URL).mock(return_value=httpx.Response(200, json=resp))
    with pytest.raises(TmpApiError, match="unexpected issueLayoutConfiguration typename"):
        await _dialect().read_layout(project_id="10001", issuetype_id=10007)


@pytest.mark.asyncio
@respx.mock
async def test_read_layout_raises_on_unrecognized_container_type():
    resp = _layout_response()
    resp["data"]["issueLayoutConfiguration"]["issueLayoutResult"]["containers"][0]["containerType"] = "WEIRD"
    respx.post(GIRA_URL).mock(return_value=httpx.Response(200, json=resp))
    with pytest.raises(TmpApiError, match="unrecognized containerType"):
        await _dialect().read_layout(project_id="10001", issuetype_id=10007)


@pytest.mark.asyncio
@respx.mock
async def test_read_layout_raises_when_no_current_project_owner():
    resp = _layout_response()
    resp["data"]["issueLayoutConfiguration"]["issueLayoutResult"]["usageInfo"]["edges"][0]["currentProject"] = False
    respx.post(GIRA_URL).mock(return_value=httpx.Response(200, json=resp))
    with pytest.raises(TmpApiError, match="no currentProject edge"):
        await _dialect().read_layout(project_id="10001", issuetype_id=10007)


# ── create_worktype / delete_worktype ──────────────────────────────────
ISSUETYPE_URL = f"{SIMPLIFIED_URL}/project/10001/settings/issuetype"


@pytest.mark.asyncio
@respx.mock
async def test_create_worktype_parses_result_and_sends_expected_body():
    route = respx.post(ISSUETYPE_URL).mock(
        return_value=httpx.Response(
            201,
            json={"id": "10080", "name": "disco-type", "entityId": "abc", "hierarchyLevel": 0, "avatarId": 10321},
        )
    )
    worktype = await _dialect().create_worktype(
        project_id="10001", project_uuid="proj-uuid-1", name="disco-type", description="a work type"
    )
    assert worktype.id == "10080"
    assert worktype.name == "disco-type"
    assert worktype.hierarchy_level == 0
    body = json.loads(route.calls.last.request.content)
    assert body["projectUuid"] == "proj-uuid-1"
    assert body["context"] == {"issueTypeKey": "custom"}
    assert "externalUuid" in body
    assert route.calls.last.request.headers["X-Atlassian-Token"] == "no-check"


@pytest.mark.asyncio
@respx.mock
async def test_create_worktype_raises_when_no_id_in_response():
    respx.post(ISSUETYPE_URL).mock(return_value=httpx.Response(201, json={"name": "disco-type"}))
    with pytest.raises(TmpApiError, match="returned no id"):
        await _dialect().create_worktype(project_id="10001", project_uuid="proj-uuid-1", name="disco-type")


@pytest.mark.asyncio
@respx.mock
async def test_delete_worktype_checks_preflight_then_deletes():
    check_route = respx.get(f"{ISSUETYPE_URL}/checkDelete/10080").mock(
        return_value=httpx.Response(200, json={"safeToDelete": True, "issueCount": 0})
    )
    delete_route = respx.delete(f"{ISSUETYPE_URL}/10080").mock(return_value=httpx.Response(204))
    await _dialect().delete_worktype(project_id="10001", worktype_id="10080")
    assert check_route.called
    assert delete_route.called
    assert delete_route.calls.last.request.headers["X-Atlassian-Token"] == "no-check"


@pytest.mark.asyncio
@respx.mock
async def test_delete_worktype_refuses_when_not_safe():
    respx.get(f"{ISSUETYPE_URL}/checkDelete/10080").mock(
        return_value=httpx.Response(200, json={"safeToDelete": False, "issueCount": 3})
    )
    delete_route = respx.delete(f"{ISSUETYPE_URL}/10080").mock(return_value=httpx.Response(204))
    with pytest.raises(TmpApiError, match="not safe to delete"):
        await _dialect().delete_worktype(project_id="10001", worktype_id="10080")
    assert not delete_route.called


# ── write_layout ─────────────────────────────────────────────────────────
def _sample_layout() -> TmpLayout:
    return TmpLayout(
        layout_id="c6b31d04-d184-48ef-877e-a11908f07576",
        owner=TmpLayoutOwner(
            id="10007", name="Task", description="tasks tasks tasks", avatar_id="10318", icon_url="https://.../10318"
        ),
        items=(
            TmpLayoutItem(
                field_id="assignee",
                key="assignee",
                name="Assignee",
                type_key="assignee",
                custom=False,
                global_=True,
                required=False,
                section="primary",
                position=100,
                external_uuid="76e94278-...",
                description="",
                operations={"editable": False},
                provider={"key": "jira-platform", "name": "Jira"},
            ),
            TmpLayoutItem(
                field_id="summary",
                key="summary",
                name="Summary",
                type_key="summary",
                custom=False,
                global_=True,
                required=True,
                section="content",
                position=50,  # earlier position than assignee -> must be sent first
                external_uuid="1ddf370e-...",
                description="",
                operations={"editable": False},
                provider={"key": "jira-platform", "name": "Jira"},
            ),
        ),
    )


def _write_layout_response(owner_description: str) -> dict:
    return {
        "projectId": 10001,
        "extraDefinerId": 10007,
        "issueLayoutType": "ISSUE_VIEW",
        "owners": [
            {
                "type": "ISSUE_TYPE",
                "data": {
                    "id": 10007,
                    "externalUuid": "Optional[476e9815-...]",
                    "name": "Task",
                    "description": owner_description,
                    "avatarId": 10318,
                    "iconUrl": "https://.../10318",
                },
            }
        ],
        "issueLayoutConfig": {
            "items": [
                {
                    "type": "FIELD",
                    "key": "summary",
                    "sectionType": "CONTENT",
                    "data": {"key": "summary", "name": "Summary", "type": "summary", "custom": False, "global": True},
                },
                {
                    "type": "FIELD",
                    "key": "assignee",
                    "sectionType": "PRIMARY",
                    "data": {
                        "key": "assignee",
                        "name": "Assignee",
                        "type": "assignee",
                        "custom": False,
                        "global": True,
                    },
                },
            ]
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_write_layout_sorts_items_by_position_and_parses_response():
    layout = _sample_layout()
    route = respx.put(f"{ISSUE_LAYOUTS_URL}/{layout.layout_id}").mock(
        return_value=httpx.Response(200, json=_write_layout_response("tasks tasks tasks"))
    )
    written = await _dialect().write_layout(project_id="10001", issuetype_id=10007, layout=layout)

    body = json.loads(route.calls.last.request.content)
    sent_items = body["issueLayoutConfig"]["items"]
    assert [i["key"] for i in sent_items] == ["summary", "assignee"]  # position 50 before 100
    assert body["projectId"] == 10001
    assert body["extraDefinerId"] == 10007
    assert route.calls.last.request.headers["X-Atlassian-Token"] == "no-check"

    assert written.owner.description == "tasks tasks tasks"
    assert [i.field_id for i in written.items] == ["summary", "assignee"]
    assert written.items[0].section == "content"
    assert written.items[1].section == "primary"


@pytest.mark.asyncio
@respx.mock
async def test_write_layout_raises_when_no_owners_in_response():
    layout = _sample_layout()
    respx.put(f"{ISSUE_LAYOUTS_URL}/{layout.layout_id}").mock(
        return_value=httpx.Response(200, json={"issueLayoutConfig": {"items": []}})
    )
    with pytest.raises(TmpApiError, match="has no owners"):
        await _dialect().write_layout(project_id="10001", issuetype_id=10007, layout=layout)


# ── resolve_project / list_worktypes / reflect ─────────────────────────
def _project_by_key_response(project_id: int, uuid: str, key: str, name: str, style: str) -> dict:
    return {
        "data": {
            "jira_projectByIdOrKey": {
                "projectId": project_id,
                "key": key,
                "uuid": uuid,
                "name": name,
                "projectType": "software",
                "projectStyle": style,
            }
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_resolve_project_returns_context():
    respx.get(TENANT_INFO_URL).mock(return_value=httpx.Response(200, json={"cloudId": "cloud-1"}))
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_project_by_key_response(
                10001, "proj-uuid-1", "VM", "Vulnerability Management", "TEAM_MANAGED_PROJECT"
            ),
        )
    )
    ctx = await _dialect().resolve_project(project_key="VM")
    assert ctx.cloud_id == "cloud-1"
    assert ctx.project_id == "10001"
    assert ctx.project_uuid == "proj-uuid-1"
    assert ctx.key == "VM"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_project_rejects_non_team_managed():
    respx.get(TENANT_INFO_URL).mock(return_value=httpx.Response(200, json={"cloudId": "cloud-1"}))
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200, json=_project_by_key_response(10002, "proj-uuid-2", "CMP", "Classic Project", "CLASSIC_PROJECT")
        )
    )
    with pytest.raises(TmpApiError, match="not TEAM_MANAGED_PROJECT"):
        await _dialect().resolve_project(project_key="CMP")


@pytest.mark.asyncio
@respx.mock
async def test_resolve_project_caches_cloud_id_across_calls():
    tenant_route = respx.get(TENANT_INFO_URL).mock(return_value=httpx.Response(200, json={"cloudId": "cloud-1"}))
    respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_project_by_key_response(
                10001, "proj-uuid-1", "VM", "Vulnerability Management", "TEAM_MANAGED_PROJECT"
            ),
        )
    )
    dialect = _dialect()
    await dialect.resolve_project(project_key="VM")
    await dialect.resolve_project(project_key="VM")
    assert tenant_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_list_worktypes_parses_response():
    respx.get(ISSUETYPE_PROJECT_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "10007", "name": "Task", "description": "Tasks track small pieces of work.", "subtask": False},
                {"id": "10008", "name": "Subtask", "description": "", "subtask": True},
            ],
        )
    )
    worktypes = await _dialect().list_worktypes(project_id="10001")
    assert [w.id for w in worktypes] == ["10007", "10008"]
    assert worktypes[0].project_scoped is True
    assert worktypes[1].subtask is True


@pytest.mark.asyncio
@respx.mock
async def test_list_worktypes_raises_on_non_list_response():
    respx.get(ISSUETYPE_PROJECT_URL).mock(return_value=httpx.Response(200, json={"error": "nope"}))
    with pytest.raises(TmpApiError, match="returned non-list"):
        await _dialect().list_worktypes(project_id="10001")


def _gateway_dispatch(responses: dict[str, dict]):
    """respx side_effect: route by operationName, since resolve_project and
    enumerate_fields both POST to the same gateway URL."""

    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        op = body.get("operationName")
        if op not in responses:
            raise AssertionError(f"no mocked response for operationName {op!r}")
        return httpx.Response(200, json=responses[op])

    return _respond


@pytest.mark.asyncio
@respx.mock
async def test_reflect_assembles_snapshot_and_layouts():
    select_type = "com.atlassian.jira.plugin.system.customfieldtypes:select"
    team_type = "com.atlassian.jira.plugin.system.customfieldtypes:atlassian-team"
    respx.get(TENANT_INFO_URL).mock(return_value=httpx.Response(200, json={"cloudId": "cloud-1"}))
    respx.get(f"{API_ROOT}/serverInfo").mock(
        return_value=httpx.Response(200, json={"deploymentType": "Cloud", "version": "1001.0.0", "baseUrl": BASE})
    )
    respx.post(GATEWAY_URL).mock(
        side_effect=_gateway_dispatch(
            {
                "StintTmpResolveProject": _project_by_key_response(
                    10001, "proj-uuid-1", "VM", "Vulnerability Management", "TEAM_MANAGED_PROJECT"
                ),
                "StintTmpFieldEnumeration": _enumeration_response(
                    [
                        _field_node(
                            "customfield_10179",
                            "test-options",
                            "PROJECT",
                            select_type,
                            options=[{"optionId": 10094, "value": "test1", "color": None}],
                        ),
                        # a GLOBAL (shared, CMP-visible) field: reflect must exclude it
                        _field_node("customfield_10001", "Team", "GLOBAL", team_type),
                    ]
                ),
            }
        )
    )
    respx.get(ISSUETYPE_PROJECT_URL).mock(
        return_value=httpx.Response(200, json=[{"id": "10007", "name": "Task", "description": "", "subtask": False}])
    )
    respx.post(GIRA_URL).mock(return_value=httpx.Response(200, json=_layout_response()))

    tmp_snapshot = await _dialect().reflect(project_key="VM")

    snap = tmp_snapshot.snapshot
    assert snap.server_info.version == "1001.0.0"
    assert list(snap.custom_fields) == ["customfield_10179"]  # GLOBAL field excluded
    assert snap.custom_fields["customfield_10179"].options == {"test1": "10094"}
    assert "10007" in snap.issuetypes
    assert snap.issuetypes["10007"].project_scoped is True
    assert snap.projects["VM"].id == "10001"
    assert snap.projects["VM"].style == "next-gen"
    assert tmp_snapshot.layouts["10007"].layout_id == "c6b31d04-d184-48ef-877e-a11908f07576"
