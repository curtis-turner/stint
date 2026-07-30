"""TMP dialect build phase 5: reconcile (desired vs reflected+state) and the
TMP-local op set.

Reuses the same schema classes (Project/IssueType/CustomField) CMP uses --
there is no separate TMP schema DSL. HTTP is respx-mocked against the same
shapes exercised in test_m9_tmp_dialect.py; this file focuses on the
plan/apply layer built on top.
"""

import json
from typing import Annotated, Literal

import httpx
import pytest
import respx

from stint import CustomField, IssueType, Project
from stint.client.auth import APITokenAuth
from stint.client.http import JiraHTTPClient
from stint.dialects.jira.tmp import TmpDialect
from stint.dialects.jira.tmp.desired import TmpDesired, TmpDesiredField, TmpDesiredWorkType, build_tmp_desired
from stint.dialects.jira.tmp.graphql import FIELDS_GATEWAY_PATH, LAYOUT_READ_PATH
from stint.dialects.jira.tmp.internal_rest import ISSUE_LAYOUTS_PATH, SIMPLIFIED_PATH
from stint.dialects.jira.tmp.models import TmpLayout, TmpLayoutItem, TmpLayoutOwner, TmpProjectContext, TmpSnapshot
from stint.dialects.jira.tmp.ops import (
    TmpApplyContext,
    tmp_delete_field,
    tmp_set_layout,
    tmp_upsert_field,
    tmp_upsert_worktype,
)
from stint.dialects.jira.tmp.reconcile import (
    CreateField,
    CreateWorkType,
    DeleteField,
    DeleteWorkType,
    SetLayout,
    UpdateField,
    apply_tmp_plan,
    plan_tmp,
    sort_tmp_changes,
)
from stint.dialects.jira.tmp.state import TmpState
from stint.fields import SelectField, TextField
from stint.registry import registry
from stint.state.snapshot import CustomFieldSnapshot, ServerInfoSnapshot, Snapshot

BASE = "https://cumulusec.atlassian.net"
GATEWAY_URL = f"{BASE}{FIELDS_GATEWAY_PATH}"
GIRA_URL = f"{BASE}{LAYOUT_READ_PATH}"
SIMPLIFIED_URL = f"{BASE}{SIMPLIFIED_PATH}"
ISSUE_LAYOUTS_URL = f"{BASE}{ISSUE_LAYOUTS_PATH}"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    yield
    registry.reset()


def _client() -> JiraHTTPClient:
    return JiraHTTPClient(BASE, auth=APITokenAuth(email="you@example.com", token="tok"))


def _ctx(state: TmpState | None = None) -> TmpApplyContext:
    return TmpApplyContext(
        dialect=TmpDialect(_client()),
        project=TmpProjectContext(
            cloud_id="cloud-1",
            project_id="10001",
            project_uuid="proj-uuid-1",
            key="VM",
            name="Vulnerability Management",
        ),
        state=state or TmpState(),
    )


def _empty_snapshot() -> Snapshot:
    return Snapshot(server_info=ServerInfoSnapshot(deployment_type="Cloud", version="x", base_url=BASE))


def _make_project() -> type[Project]:
    sev_cf = CustomField(alias="severity", name="Severity", type=SelectField, options=["S1", "S2"])
    root_cause_cf = CustomField(alias="root_cause", name="Root Cause", type=TextField)

    class _Bug(IssueType):
        __alias__ = "bug"
        __title__ = "Bug"
        __description__ = "A bug"

        severity: Annotated[Literal["S1", "S2"], sev_cf]
        root_cause: Annotated[str, root_cause_cf]

    class _Vuln(Project):
        __key__ = "VM"
        __style__ = "team-managed"
        __issuetypes__ = [_Bug]

    return _Vuln


# ── build_tmp_desired ────────────────────────────────────────────────
def test_build_tmp_desired_collects_fields_and_worktypes():
    desired = build_tmp_desired(_make_project())
    assert desired.project_key == "VM"
    assert set(desired.fields) == {"severity", "root_cause"}
    assert desired.fields["severity"].options == ("S1", "S2")
    assert desired.fields["severity"].type_key == SelectField.jira_type_id
    assert desired.fields["root_cause"].options == ()
    assert set(desired.worktypes) == {"bug"}
    assert desired.worktypes["bug"].name == "Bug"
    assert desired.worktypes["bug"].description == "A bug"
    assert desired.worktypes["bug"].field_aliases == ("severity", "root_cause")


# ── plan_tmp (pure) ──────────────────────────────────────────────────
def test_plan_tmp_creates_everything_when_state_empty():
    desired = build_tmp_desired(_make_project())
    changes = plan_tmp(desired, TmpSnapshot(snapshot=_empty_snapshot()), TmpState())
    assert CreateField("severity") in changes
    assert CreateField("root_cause") in changes
    assert CreateWorkType("bug") in changes
    assert SetLayout("bug") in changes
    assert not any(isinstance(c, DeleteField | DeleteWorkType) for c in changes)


def test_plan_tmp_updates_field_when_options_drift():
    desired = build_tmp_desired(_make_project())
    snap = _empty_snapshot()
    snap.custom_fields["customfield_1"] = CustomFieldSnapshot(
        id="customfield_1", name="Severity", type_id=SelectField.jira_type_id, options={"S1": "10001"}
    )  # missing S2
    state = TmpState(fields={"severity": "customfield_1"})
    changes = plan_tmp(desired, TmpSnapshot(snapshot=snap), state)
    assert UpdateField("severity") in changes


def test_plan_tmp_no_changes_when_everything_matches():
    desired = build_tmp_desired(_make_project())
    snap = _empty_snapshot()
    snap.custom_fields["customfield_1"] = CustomFieldSnapshot(
        id="customfield_1", name="Severity", type_id=SelectField.jira_type_id, options={"S1": "1", "S2": "2"}
    )
    snap.custom_fields["customfield_2"] = CustomFieldSnapshot(
        id="customfield_2", name="Root Cause", type_id=TextField.jira_type_id, options={}
    )
    common = {
        "external_uuid": "",
        "description": "",
        "operations": {},
        "provider": {},
    }
    layout = TmpLayout(
        layout_id="layout-1",
        owner=TmpLayoutOwner(id="10007", name="Bug", description="A bug", avatar_id="1", icon_url="x"),
        items=(
            TmpLayoutItem(
                field_id="customfield_1",
                key="customfield_1",
                name="Severity",
                type_key=SelectField.jira_type_id,
                custom=True,
                global_=False,
                required=False,
                section="primary",
                position=100,
                **common,
            ),
            TmpLayoutItem(
                field_id="customfield_2",
                key="customfield_2",
                name="Root Cause",
                type_key=TextField.jira_type_id,
                custom=True,
                global_=False,
                required=False,
                section="primary",
                position=200,
                **common,
            ),
        ),
    )
    snapshot = TmpSnapshot(snapshot=snap, layouts={"10007": layout})
    state = TmpState(fields={"severity": "customfield_1", "root_cause": "customfield_2"}, worktypes={"bug": "10007"})
    assert plan_tmp(desired, snapshot, state) == []


def test_plan_tmp_deletes_only_when_allow_delete():
    desired = TmpDesired(project_key="VM")  # nothing declared
    snapshot = TmpSnapshot(snapshot=_empty_snapshot())
    state = TmpState(fields={"gone": "customfield_9"}, worktypes={"gone_wt": "10099"})
    assert plan_tmp(desired, snapshot, state) == []
    changes = plan_tmp(desired, snapshot, state, allow_delete=True)
    assert DeleteField("gone") in changes
    assert DeleteWorkType("gone_wt") in changes


def test_sort_tmp_changes_orders_by_apply_phase():
    changes = [
        DeleteField("a"),
        SetLayout("wt"),
        CreateField("a"),
        CreateWorkType("wt"),
        DeleteWorkType("wt2"),
        UpdateField("b"),
    ]
    kinds = [type(c).__name__ for c in sort_tmp_changes(changes)]
    assert kinds.index("CreateWorkType") < kinds.index("SetLayout")
    assert kinds.index("SetLayout") < kinds.index("DeleteWorkType")
    assert kinds.index("DeleteWorkType") < kinds.index("DeleteField")


# ── ops (respx-mocked) ───────────────────────────────────────────────
def _field_response(field_id: str, name: str) -> dict:
    return {
        "data": {
            "jira": {
                "createCustomFieldInProjectAndAddToAllIssueTypes": {
                    "success": True,
                    "fieldAssociationWithIssueTypes": {
                        "field": {"fieldId": field_id, "name": name, "description": "", "scope": "PROJECT"},
                        "fieldOptions": {"edges": []},
                    },
                }
            }
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_tmp_upsert_field_creates_when_absent():
    respx.post(GATEWAY_URL).mock(return_value=httpx.Response(200, json=_field_response("customfield_1", "Severity")))
    ctx = _ctx()
    desired = TmpDesiredField(
        alias="severity", name="Severity", type_key=SelectField.jira_type_id, options=("S1", "S2")
    )
    await tmp_upsert_field(ctx, desired, current_field_id=None)
    assert ctx.state.fields["severity"] == "customfield_1"


@pytest.mark.asyncio
@respx.mock
async def test_tmp_upsert_field_preserves_existing_option_ids_on_edit():
    route = respx.post(GATEWAY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "jira": {
                        "editCustomField": {
                            "success": True,
                            "fieldAssociationWithIssueTypes": {
                                "field": {
                                    "fieldId": "customfield_1",
                                    "name": "Severity",
                                    "description": "",
                                    "scope": "PROJECT",
                                },
                                "fieldOptions": {"edges": []},
                            },
                        }
                    }
                }
            },
        )
    )
    ctx = _ctx()
    desired = TmpDesiredField(
        alias="severity", name="Severity", type_key=SelectField.jira_type_id, options=("S1", "S2")
    )
    await tmp_upsert_field(ctx, desired, current_field_id="customfield_1", current_options={"S1": "10001"})
    body = json.loads(route.calls.last.request.content)
    assert 'optionId: "10001"' in body["query"]  # S1 keeps its existing id
    assert "optionId: null" in body["query"]  # S2 is new


@pytest.mark.asyncio
@respx.mock
async def test_tmp_delete_field_noop_if_absent_from_state():
    ctx = _ctx()
    await tmp_delete_field(ctx, "nope")  # no route registered; a real call would fail loudly


@pytest.mark.asyncio
@respx.mock
async def test_tmp_upsert_worktype_noop_if_already_in_state():
    ctx = _ctx(TmpState(worktypes={"bug": "10007"}))
    await tmp_upsert_worktype(ctx, TmpDesiredWorkType(alias="bug", name="Bug"))
    assert ctx.state.worktypes["bug"] == "10007"


@pytest.mark.asyncio
@respx.mock
async def test_tmp_set_layout_prunes_undeclared_fields_and_resyncs_owner():
    common = {"external_uuid": "u", "description": "", "operations": {}, "provider": {}}
    current = TmpLayout(
        layout_id="layout-1",
        owner=TmpLayoutOwner(id="10007", name="Bug", description="old desc", avatar_id="1", icon_url="x"),
        items=(
            TmpLayoutItem(
                field_id="summary",
                key="summary",
                name="Summary",
                type_key="summary",
                custom=False,
                global_=True,
                required=True,
                section="content",
                position=100,
                **common,
            ),
            TmpLayoutItem(
                field_id="customfield_1",
                key="customfield_1",
                name="Severity",
                type_key=SelectField.jira_type_id,
                custom=True,
                global_=False,
                required=False,
                section="primary",
                position=100,
                **common,
            ),
            TmpLayoutItem(
                field_id="customfield_2",
                key="customfield_2",
                name="Old Field",
                type_key=TextField.jira_type_id,
                custom=True,
                global_=False,
                required=False,
                section="primary",
                position=200,
                **common,
            ),
        ),
    )
    route = respx.put(f"{ISSUE_LAYOUTS_URL}/layout-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "owners": [
                    {
                        "type": "ISSUE_TYPE",
                        "data": {"id": 10007, "name": "Bug", "description": "new desc", "avatarId": 1, "iconUrl": "x"},
                    }
                ],
                "issueLayoutConfig": {
                    "items": [
                        {
                            "type": "FIELD",
                            "key": "summary",
                            "sectionType": "CONTENT",
                            "data": {
                                "key": "summary",
                                "name": "Summary",
                                "type": "summary",
                                "custom": False,
                                "global": True,
                            },
                        },
                        {
                            "type": "FIELD",
                            "key": "customfield_1",
                            "sectionType": "PRIMARY",
                            "data": {
                                "key": "customfield_1",
                                "name": "Severity",
                                "type": SelectField.jira_type_id,
                                "custom": True,
                                "global": False,
                            },
                        },
                    ]
                },
            },
        )
    )
    ctx = _ctx(TmpState(fields={"severity": "customfield_1"}, worktypes={"bug": "10007"}))
    desired = TmpDesiredWorkType(alias="bug", name="Bug", description="new desc", field_aliases=("severity",))
    await tmp_set_layout(ctx, "bug", desired, current)

    body = json.loads(route.calls.last.request.content)
    sent_keys = [item["key"] for item in body["issueLayoutConfig"]["items"]]
    assert sent_keys == ["summary", "customfield_1"]  # customfield_2 (undeclared) pruned
    assert body["owners"][0]["data"]["description"] == "new desc"
    assert ctx.state.layout_ids["bug"] == "layout-1"


# ── apply_tmp_plan end to end ────────────────────────────────────────
def _field_create_dispatch():
    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = body["query"]
        if "Severity" in query:
            return httpx.Response(200, json=_field_response("customfield_1", "Severity"))
        if "Root Cause" in query:
            return httpx.Response(200, json=_field_response("customfield_2", "Root Cause"))
        raise AssertionError(f"unexpected field-create query: {query[:200]!r}")

    return _respond


@pytest.mark.asyncio
@respx.mock
async def test_apply_tmp_plan_end_to_end():
    desired = build_tmp_desired(_make_project())
    snapshot = TmpSnapshot(snapshot=_empty_snapshot())
    state = TmpState()
    changes = plan_tmp(desired, snapshot, state)

    respx.post(GATEWAY_URL).mock(side_effect=_field_create_dispatch())
    respx.post(f"{SIMPLIFIED_URL}/project/10001/settings/issuetype").mock(
        return_value=httpx.Response(201, json={"id": "10007", "name": "Bug", "avatarId": 10321, "hierarchyLevel": 0})
    )
    respx.post(GIRA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueLayoutConfiguration": {
                        "__typename": "JiraIssueLayoutConfigurationResult",
                        "issueLayoutResult": {
                            "id": "layout-x",
                            "name": "VM-Bug",
                            "usageInfo": {
                                "edges": [
                                    {
                                        "currentProject": True,
                                        "node": {
                                            "layoutOwners": [
                                                {
                                                    "__typename": "JiraIssueLayoutIssueTypeOwner",
                                                    "id": "10007",
                                                    "name": "Bug",
                                                    "description": "A bug",
                                                    "avatarId": "10321",
                                                    "iconUrl": "x",
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                            "containers": [],
                        },
                        "metadata": {"configuration": {"items": {"nodes": []}}},
                    }
                }
            },
        )
    )
    respx.put(f"{ISSUE_LAYOUTS_URL}/layout-x").mock(
        return_value=httpx.Response(
            200,
            json={
                "owners": [
                    {
                        "type": "ISSUE_TYPE",
                        "data": {"id": 10007, "name": "Bug", "description": "A bug", "avatarId": 10321, "iconUrl": "x"},
                    }
                ],
                "issueLayoutConfig": {"items": []},
            },
        )
    )

    ctx = _ctx(state)
    await apply_tmp_plan(ctx, changes, desired, snapshot)

    assert ctx.state.fields["severity"] == "customfield_1"
    assert ctx.state.fields["root_cause"] == "customfield_2"
    assert ctx.state.worktypes["bug"] == "10007"
    assert ctx.state.layout_ids["bug"] == "layout-x"
