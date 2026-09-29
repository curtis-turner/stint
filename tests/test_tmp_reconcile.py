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
from stint.exceptions import TransportError
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


# ── tmp_set_layout add-missing-items branch (TMP-A) ─────────────────
def _empty_worktype_layout(layout_id: str = "layout-new") -> TmpLayout:
    """The layout of a work type that was just created: no custom items,
    only the owner's metadata. Confirmed live (see ISSUE_DRAFT.md TMP-A):
    ``read_layout`` on a freshly created work type returns zero custom
    items, and the old ``tmp_set_layout`` would write that emptiness back
    -- the field would never appear on the work type's edit screen."""
    return TmpLayout(
        layout_id=layout_id,
        owner=TmpLayoutOwner(id="10007", name="Bug", description="A bug", avatar_id="1", icon_url="x"),
        items=(),
    )


@pytest.mark.asyncio
@respx.mock
async def test_tmp_set_layout_synthesizes_items_for_new_worktype():
    """A brand-new work type's current layout has zero custom items
    (confirmed live). ``tmp_set_layout`` must NOT write that emptiness
    back: it has to add layout items for every declared custom field.

    With no snapshot to harvest from and no other work type the apply
    run knows about, this exercises the synthesis fallback. The wire
    body must carry one synthesized item per declared field, with
    ``custom=True`` and ``section=primary``."""
    route = respx.put(f"{ISSUE_LAYOUTS_URL}/layout-new").mock(
        return_value=httpx.Response(
            200,
            json={
                "owners": [
                    {
                        "type": "ISSUE_TYPE",
                        "data": {"id": 10007, "name": "Bug", "description": "A bug", "avatarId": 1, "iconUrl": "x"},
                    }
                ],
                "issueLayoutConfig": {"items": []},
            },
        )
    )
    ctx = _ctx(
        TmpState(fields={"severity": "customfield_1", "root_cause": "customfield_2"}, worktypes={"bug": "10007"})
    )
    desired = TmpDesiredWorkType(alias="bug", name="Bug", description="A bug", field_aliases=("severity", "root_cause"))
    desired_all = TmpDesired(
        project_key="VM",
        fields={
            "severity": TmpDesiredField(alias="severity", name="Severity", type_key=SelectField.jira_type_id),
            "root_cause": TmpDesiredField(alias="root_cause", name="Root Cause", type_key=TextField.jira_type_id),
        },
        worktypes={"bug": desired},
    )
    await tmp_set_layout(ctx, "bug", desired, _empty_worktype_layout(), desired_all=desired_all)

    body = json.loads(route.calls.last.request.content)
    sent = body["issueLayoutConfig"]["items"]
    assert len(sent) == 2  # both fields attached, not the empty pre-fix write
    sent_by_key = {item["key"]: item for item in sent}
    assert sent_by_key["customfield_1"]["data"]["name"] == "Severity"
    assert sent_by_key["customfield_1"]["sectionType"] == "primary"
    assert sent_by_key["customfield_1"]["data"]["custom"] is True
    assert sent_by_key["customfield_1"]["data"]["global"] is False
    assert sent_by_key["customfield_1"]["data"]["required"] is False
    # Distinct externalUuid per field so Jira treats them as distinct items.
    assert sent_by_key["customfield_1"]["data"]["externalUuid"] != sent_by_key["customfield_2"]["data"]["externalUuid"]


@pytest.mark.asyncio
@respx.mock
async def test_tmp_set_layout_harvests_template_from_snapshot():
    """If another work type in the snapshot already has the field, copy
    that item instead of synthesizing. The harvested item keeps its
    source-work-type operations/provider/externalUuid exactly as Jira
    returned them, so the wire shape round-trips without surprise."""
    sibling_layout = TmpLayout(
        layout_id="layout-sibling",
        owner=TmpLayoutOwner(id="10008", name="Other", description="", avatar_id="1", icon_url="x"),
        items=(
            TmpLayoutItem(
                field_id="customfield_1",
                key="customfield_1",
                name="Severity",
                type_key=SelectField.jira_type_id,
                custom=True,
                global_=False,
                required=False,
                section="secondary",  # distinct from default to prove it copied
                position=314,
                external_uuid="uuid-from-sibling",
                description="harvested",
                operations={"move": True},
                provider={"k": "v"},
            ),
        ),
    )
    snap = TmpSnapshot(snapshot=_empty_snapshot(), layouts={"10008": sibling_layout})

    put_route = respx.put(f"{ISSUE_LAYOUTS_URL}/layout-new").mock(
        return_value=httpx.Response(
            200,
            json={
                "owners": [
                    {
                        "type": "ISSUE_TYPE",
                        "data": {"id": 10007, "name": "Bug", "description": "A bug", "avatarId": 1, "iconUrl": "x"},
                    }
                ],
                "issueLayoutConfig": {"items": []},
            },
        )
    )
    ctx = _ctx(TmpState(fields={"severity": "customfield_1"}, worktypes={"bug": "10007"}))
    desired = TmpDesiredWorkType(alias="bug", name="Bug", description="A bug", field_aliases=("severity",))
    desired_all = TmpDesired(
        project_key="VM",
        fields={"severity": TmpDesiredField(alias="severity", name="Severity", type_key=SelectField.jira_type_id)},
        worktypes={"bug": desired},
    )
    await tmp_set_layout(ctx, "bug", desired, _empty_worktype_layout(), snapshot=snap, desired_all=desired_all)

    body = json.loads(put_route.calls.last.request.content)
    sent = body["issueLayoutConfig"]["items"]
    assert len(sent) == 1
    item = sent[0]
    # Harvested from sibling: the wire fields round-trip untouched.
    assert item["data"]["externalUuid"] == "uuid-from-sibling"
    assert item["sectionType"] == "secondary"
    assert item["data"]["operations"] == {"move": True}
    assert item["data"]["provider"] == {"k": "v"}


@pytest.mark.asyncio
@respx.mock
async def test_tmp_set_layout_falls_back_to_remote_read_when_snapshot_misses():
    """When the snapshot doesn't carry the field -- it was created in
    this same apply run, for instance -- try a fresh ``read_layout`` of
    a sibling work type the apply run knows about. First sibling that
    has it wins."""
    # Fresh sibling read returns an item for customfield_1 (positioned
    # in a PRIMARY container so the parser actually emits a TmpLayoutItem).
    respx.post(GIRA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueLayoutConfiguration": {
                        "__typename": "JiraIssueLayoutConfigurationResult",
                        "issueLayoutResult": {
                            "id": "layout-sibling",
                            "usageInfo": {
                                "edges": [
                                    {
                                        "currentProject": True,
                                        "node": {
                                            "layoutOwners": [
                                                {
                                                    "__typename": "JiraIssueLayoutIssueTypeOwner",
                                                    "id": "10008",
                                                    "name": "Other",
                                                    "description": "",
                                                    "avatarId": "1",
                                                    "iconUrl": "x",
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                            "containers": [
                                {
                                    "containerType": "PRIMARY",
                                    "items": {
                                        "nodes": [
                                            {
                                                "__typename": "JiraIssueItemFieldItem",
                                                "fieldItemId": "customfield_1",
                                                "containerPosition": 100,
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                        "metadata": {
                            "configuration": {
                                "items": {
                                    "nodes": [
                                        {
                                            "__typename": "JiraIssueLayoutFieldItemConfiguration",
                                            "fieldItemId": "customfield_1",
                                            "key": "customfield_1",
                                            "name": "Severity",
                                            "type": SelectField.jira_type_id,
                                            "custom": True,
                                            "global": False,
                                            "required": False,
                                            "externalUuid": "fresh-uuid",
                                        }
                                    ]
                                }
                            }
                        },
                    }
                }
            },
        )
    )

    put_route = respx.put(f"{ISSUE_LAYOUTS_URL}/layout-new").mock(
        return_value=httpx.Response(
            200,
            json={
                "owners": [
                    {
                        "type": "ISSUE_TYPE",
                        "data": {"id": 10007, "name": "Bug", "description": "A bug", "avatarId": 1, "iconUrl": "x"},
                    }
                ],
                "issueLayoutConfig": {"items": []},
            },
        )
    )
    ctx = _ctx(
        TmpState(
            fields={"severity": "customfield_1"},
            worktypes={"bug": "10007", "other": "10008"},  # other is the sibling we'll read
        )
    )
    desired = TmpDesiredWorkType(alias="bug", name="Bug", description="A bug", field_aliases=("severity",))
    desired_all = TmpDesired(
        project_key="VM",
        fields={"severity": TmpDesiredField(alias="severity", name="Severity", type_key=SelectField.jira_type_id)},
        worktypes={"bug": desired},
    )
    # Empty snapshot: the harvest-from-snapshot step finds nothing.
    snap = TmpSnapshot(snapshot=_empty_snapshot(), layouts={})
    await tmp_set_layout(ctx, "bug", desired, _empty_worktype_layout(), snapshot=snap, desired_all=desired_all)

    body = json.loads(put_route.calls.last.request.content)
    sent = body["issueLayoutConfig"]["items"]
    assert len(sent) == 1
    # Fresh-read item carries the UUID from the live sibling response.
    assert sent[0]["data"]["externalUuid"] == "fresh-uuid"


@pytest.mark.asyncio
@respx.mock
async def test_apply_tmp_plan_layout_items_appear_for_new_worktype_end_to_end():
    """The full end-to-end repro from ISSUE_DRAFT.md TMP-A: a brand-new
    project with one new field and one new work type that declares the
    field. Before the fix, ``apply`` silently wrote an empty layout;
    here the new layout's wire body carries the synthesized item."""
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
    put_route = respx.put(f"{ISSUE_LAYOUTS_URL}/layout-x").mock(
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

    # Two custom fields declared -> two layout items in the wire body.
    body = json.loads(put_route.calls.last.request.content)
    sent = body["issueLayoutConfig"]["items"]
    assert len(sent) == 2
    sent_field_ids = {item["data"]["key"] for item in sent}
    # Field ids (customfield_1 and customfield_2 from create dispatch) both attached.
    assert sent_field_ids == {"customfield_1", "customfield_2"}


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


# ── per-op persist (TMP-C fix-forward) ───────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_apply_tmp_plan_persists_after_each_state_mutation():
    """``ctx.persist()`` runs once per successful state mutation, so a mid-plan
    failure leaves everything that already wrote to Jira recorded on disk.
    Verifies the callback fires 4 times for the canonical end-to-end plan:
    2 field creates + 1 worktype create + 1 layout write."""
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

    checkpoints: list[TmpState] = []

    def _on_change() -> None:
        # Capture a copy of the state at each checkpoint so the assertion
        # below sees the cumulative-progress signal the CLI relies on.
        checkpoints.append(
            TmpState(
                fields=dict(state.fields),
                worktypes=dict(state.worktypes),
                layout_ids=dict(state.layout_ids),
            )
        )

    ctx = _ctx(state)
    ctx.on_state_changed = _on_change
    await apply_tmp_plan(ctx, changes, desired, snapshot)

    assert len(checkpoints) == 4
    # Each checkpoint reflects one more mutation than the previous one.
    assert checkpoints[0].fields == {"severity": "customfield_1"}
    assert "root_cause" not in checkpoints[0].fields
    assert checkpoints[1].fields == {"severity": "customfield_1", "root_cause": "customfield_2"}
    assert "bug" not in checkpoints[1].worktypes
    assert checkpoints[2].worktypes == {"bug": "10007"}
    assert "bug" not in checkpoints[2].layout_ids
    assert checkpoints[3].layout_ids == {"bug": "layout-x"}


@pytest.mark.asyncio
@respx.mock
async def test_apply_tmp_plan_state_survives_mid_plan_failure():
    """If an op raises mid-plan, every prior mutation is reflected in
    ``ctx.state`` AND each prior op's ``on_state_changed`` already ran --
    exactly the signal the CLI needs to checkpoint the file. The retry
    below then plans against the updated state and produces no duplicate
    work, which is the fix-forward contract."""
    desired = build_tmp_desired(_make_project())
    snapshot = TmpSnapshot(snapshot=_empty_snapshot())
    state = TmpState()
    changes = plan_tmp(desired, snapshot, state)

    # First field create succeeds, second fails. Subsequent routes are
    # unreachable on this run; registering them keeps the test honest.
    def _field_dispatch(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "Severity" in body["query"]:
            return httpx.Response(200, json=_field_response("customfield_1", "Severity"))
        if "Root Cause" in body["query"]:
            return httpx.Response(500, json={"errors": [{"message": "boom"}]})
        raise AssertionError(f"unexpected query: {body['query'][:80]!r}")

    respx.post(GATEWAY_URL).mock(side_effect=_field_dispatch)
    respx.post(f"{SIMPLIFIED_URL}/project/10001/settings/issuetype").mock(
        return_value=httpx.Response(201, json={"id": "10007", "name": "Bug", "avatarId": 10321, "hierarchyLevel": 0})
    )

    checkpoints: list[TmpState] = []
    ctx = _ctx(state)

    def _on_change() -> None:
        checkpoints.append(
            TmpState(
                fields=dict(state.fields),
                worktypes=dict(state.worktypes),
                layout_ids=dict(state.layout_ids),
            )
        )

    ctx.on_state_changed = _on_change
    with pytest.raises(TransportError):
        await apply_tmp_plan(ctx, changes, desired, snapshot)

    # First field was committed AND persisted; second was neither.
    assert state.fields == {"severity": "customfield_1"}
    assert "root_cause" not in state.fields
    assert state.worktypes == {}
    assert state.layout_ids == {}
    assert len(checkpoints) == 1

    # Retry sees the prior success and plans no duplicate work for it.
    changes_retry = plan_tmp(desired, snapshot, state)
    assert CreateField("severity") not in changes_retry
    assert CreateField("root_cause") in changes_retry  # still missing
