"""TMP dialect build phase 2: fields-gateway client, field CRUD, and reads.

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
from stint.dialects.jira.tmp.models import TmpFieldOption
from stint.exceptions import TmpApiError

BASE = "https://cumulusec.atlassian.net"
GATEWAY_URL = f"{BASE}{FIELDS_GATEWAY_PATH}"
GIRA_URL = f"{BASE}{LAYOUT_READ_PATH}"


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


def _field_node(field_id: str, name: str, scope: str, type_key: str) -> dict:
    return {"node": {"field": {"fieldId": field_id, "name": name, "scope": scope, "typeKey": type_key}}}


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
