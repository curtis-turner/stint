"""Workflows, workflow schemes, and the transition lockout guard.

Covers the schema declarations, reflection parsing, the diff families
(greenfield create / adoption / declarative update with preservation), emit
rendering, the ops (create/adopt/update, scheme create/associate/switch with
required-mapping discovery, task polling), and the lockout evaluators.
All HTTP is respx-mocked.
"""

from __future__ import annotations

import textwrap
from typing import Any

import httpx
import pytest
import respx

from stint import (
    ConfigurationError,
    IssueType,
    PATAuth,
    Project,
    ProjectRole,
    RegistryError,
    StateFile,
    Status,
    Transition,
    User,
    Workflow,
    WorkflowScheme,
    create_engine,
    op,
    registry,
    validate,
)
from stint.autogen.desired import build_desired_snapshot
from stint.autogen.diff import (
    AdoptWorkflow,
    AdoptWorkflowScheme,
    CreateWorkflow,
    CreateWorkflowScheme,
    DeleteWorkflow,
    SetProjectWorkflowScheme,
    UpdateWorkflow,
    diff,
)
from stint.autogen.emit import _render_one, sort_changes
from stint.dialects.jira import common
from stint.engine import Engine
from stint.lockout import account_can_transition, lost_transitions, restrict_rule_parameters
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.state.file import ProjectMapping, WorkflowMapping, WorkflowSchemeMapping
from stint.state.snapshot import (
    ProjectSnapshot,
    ServerInfoSnapshot,
    Snapshot,
    WorkflowSchemeSnapshot,
)
from tests._stubs import paginated, stub_project_roles_and_permission_schemes, stub_workflows

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    yield
    registry.reset()


# ── Schema declarations ──────────────────────────────────────────────
def _flow(**overrides: Any) -> Workflow:
    open_ = Status("Open", "new")
    prog = Status("In Progress")
    done = Status("Done", "done")
    kwargs: dict[str, Any] = {
        "alias": "tracker_flow",
        "name": "Tracker Workflow",
        "statuses": [open_, prog, done],
        "initial": open_,
        "transitions": [
            Transition("Start", from_=[open_], to=prog),
            Transition("Close", from_=[prog], to=done, restrict_to=[ProjectRole("Administrators")]),
        ],
    }
    kwargs.update(overrides)
    return Workflow(**kwargs)


def test_workflow_registers():
    workflow = _flow()
    assert registry.workflows["tracker_flow"] is workflow
    assert workflow.name == "Tracker Workflow"


def test_workflow_rejects_initial_not_in_statuses():
    with pytest.raises(RegistryError, match="initial status"):
        _flow(initial=Status("Ghost"))


def test_workflow_rejects_undeclared_transition_statuses():
    with pytest.raises(RegistryError, match="target status"):
        _flow(transitions=[Transition("Close", from_=[Status("In Progress")], to=Status("Nowhere"))])
    with pytest.raises(RegistryError, match="source status"):
        _flow(transitions=[Transition("Close", from_=[Status("Nowhere")], to=Status("Done", "done"))])


def test_workflow_rejects_duplicate_names():
    open_ = Status("Open", "new")
    prog = Status("In Progress")
    with pytest.raises(RegistryError, match="duplicate status names"):
        _flow(statuses=[open_, Status("Open")])
    with pytest.raises(RegistryError, match="duplicate transition names"):
        _flow(transitions=[Transition("Go", from_=[open_], to=prog), Transition("Go", from_=[prog], to=open_)])


def test_status_rejects_unknown_category():
    with pytest.raises(RegistryError, match="unknown category"):
        Status("Weird", category="limbo")


def test_workflow_scheme_registers_and_validates():
    scheme = WorkflowScheme(alias="tracker_wfs", name="Tracker WFS", default=_flow())
    assert registry.workflow_schemes["tracker_wfs"] is scheme
    with pytest.raises(RegistryError, match="default workflow is required"):
        WorkflowScheme(alias="bad", default=None)  # the None is caught by __post_init__
    with pytest.raises(RegistryError, match="must be a Workflow"):
        WorkflowScheme(alias="bad2", default=_flow(), mappings={"ticket": "not-a-workflow"})  # ty: ignore[invalid-argument-type]


def test_project_rejects_unknown_mapping_key():
    class Bug(IssueType):
        __alias__ = "bug"

    bad = WorkflowScheme(alias="wfs_bad", default=_flow(), mappings={"ghost": _flow(alias="ghost_flow")})
    with pytest.raises(ConfigurationError, match="not one of the project's declared issuetype aliases"):

        class Bad(Project):
            __key__ = "BAD"
            __issuetypes__ = [Bug]
            __workflow_scheme__ = bad


def test_validate_flags_undeclared_project_role_restrict():
    class Bug(IssueType):
        __alias__ = "bug"

    class Tracked(Project):
        __key__ = "TRK"
        __issuetypes__ = [Bug]
        __roles__ = {"Administrators": [User("svc@example.com")]}
        __workflow_scheme__ = WorkflowScheme(
            alias="wfs",
            default=_flow(
                transitions=[
                    Transition(
                        "Close",
                        from_=[Status("In Progress")],
                        to=Status("Done", "done"),
                        restrict_to=[ProjectRole("TypoRole")],
                    )
                ]
            ),
        )

    problems = validate()
    assert any("TypoRole" in p and "ProjectRole" in p for p in problems)


# ── Reflection parsing ───────────────────────────────────────────────
def _search_payload() -> dict[str, Any]:
    return {
        "values": [
            {
                "id": "c5ef565c-1b1e-427e-bc3b-e677b0dc027c",
                "name": "Tracker Workflow",
                "description": "",
                "scope": {"type": "GLOBAL"},
                "version": {"id": "v-1", "versionNumber": 3},
                "statuses": [
                    {"statusReference": "10001", "deprecated": False},
                    {"statusReference": "10002", "deprecated": False},
                ],
                "transitions": [
                    {
                        "id": "1",
                        "name": "Create",
                        "type": "INITIAL",
                        "toStatusReference": "10001",
                        "links": [],
                    },
                    {
                        "id": "31",
                        "name": "Done",
                        "type": "DIRECTED",
                        "toStatusReference": "10002",
                        "links": [{"fromStatusReference": "10001"}],
                        "conditions": {
                            "operation": "ALL",
                            "conditions": [
                                {
                                    "ruleKey": "system:restrict-issue-transition",
                                    "parameters": {"permissionKeys": "ADMINISTER_PROJECTS"},
                                }
                            ],
                        },
                        "validators": [{"ruleKey": "system:validate-field-value", "parameters": {}}],
                        "actions": [{"ruleKey": "system:update-field", "parameters": {}}],
                    },
                ],
            }
        ],
        "statuses": [
            {"id": "10001", "name": "Open", "statusCategory": "TODO", "scope": {"type": "GLOBAL"}},
            {"id": "10002", "name": "Done", "statusCategory": "DONE", "scope": {"type": "GLOBAL"}},
        ],
        "isLast": True,
    }


def test_parse_workflow_resolves_references_and_rules():
    payload = _search_payload()
    statuses = common.parse_workflow_status_map(payload["statuses"])
    workflow = common.parse_workflow(payload["values"][0], statuses)
    assert workflow.id == "c5ef565c-1b1e-427e-bc3b-e677b0dc027c"
    assert [s.name for s in workflow.statuses] == ["Open", "Done"]
    assert workflow.version_id == "v-1" and workflow.version_number == 3
    initial, done_transition = workflow.transitions[0], workflow.transitions[1]
    assert initial.is_initial and initial.to_status == "Open"
    assert done_transition.from_statuses == ("Open",) and done_transition.to_status == "Done"
    assert restrict_rule_parameters(done_transition.conditions) == {"permissionKeys": "ADMINISTER_PROJECTS"}
    assert done_transition.validators[0].raw["ruleKey"] == "system:validate-field-value"
    assert done_transition.postfunctions[0].raw["ruleKey"] == "system:update-field"


def test_parse_workflow_scheme_classic_shape():
    scheme = common.parse_workflow_scheme(
        {
            "id": 10000,
            "name": "Tracker WFS",
            "description": "",
            "defaultWorkflow": "Tracker Workflow",
            "issueTypeMappings": {"10001": "Tracker Workflow"},
        }
    )
    assert scheme.id == "10000"
    assert scheme.default_workflow == "Tracker Workflow"
    assert scheme.mappings == {"10001": "Tracker Workflow"}


# ── Desired snapshot ─────────────────────────────────────────────────
def _schema() -> tuple[type[Project], Workflow, WorkflowScheme]:
    workflow = _flow()
    scheme = WorkflowScheme(alias="tracker_wfs", name="Tracker WFS", default=workflow)

    class Bug(IssueType):
        __alias__ = "bug"

    class Tracked(Project):
        __key__ = "TRK"
        __issuetypes__ = [Bug]
        __workflow_scheme__ = scheme

    return Tracked, workflow, scheme


def test_desired_snapshot_carries_workflows_and_scheme():
    project_cls, _workflow, _scheme = _schema()
    desired = build_desired_snapshot()
    flow = desired.workflows["tracker_flow"]
    assert flow.name == "Tracker Workflow"
    assert flow.initial == "Open"
    assert [s.name for s in flow.statuses] == ["Open", "In Progress", "Done"]
    close = flow.transitions[1]
    assert close.to_status == "Done"
    assert (
        close.restrict_to[0].holder_type == "projectRole" and close.restrict_to[0].holder_parameter == "Administrators"
    )
    scheme = desired.workflow_schemes["tracker_wfs"]
    assert scheme.default == "tracker_flow"
    assert desired.projects["TRK"].workflow_scheme == "tracker_wfs"


# ── Diff ─────────────────────────────────────────────────────────────
def _snapshot() -> Snapshot:
    snap = Snapshot(server_info=ServerInfoSnapshot(deployment_type="Cloud", version="1", base_url=BASE))
    snap.projects["TRK"] = ProjectSnapshot(id="10010", key="TRK", name="TRK", style="classic")
    return snap


def _state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["TRK"] = ProjectMapping(id="10010", key="TRK")
    return state


def test_diff_greenfield_emits_create_family():
    project_cls, _workflow, _scheme = _schema()
    state = _state()
    del state.projects["TRK"]  # the project is created by this same migration
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=state)
    changes = sort_changes(result.changes)
    kinds = [type(c).__name__ for c in changes]
    assert kinds.index("CreateWorkflow") < kinds.index("CreateWorkflowScheme")
    assert kinds.index("CreateWorkflowScheme") < kinds.index("SetProjectWorkflowScheme")
    create = next(c for c in changes if isinstance(c, CreateWorkflow))
    assert create.name == "Tracker Workflow"
    assert create.initial == "Open"
    bind = next(c for c in changes if isinstance(c, SetProjectWorkflowScheme))
    assert bind.live is False  # project created in the same migration
    assert not result.warnings


def test_diff_adopts_existing_workflow_and_scheme_by_name():
    project_cls, workflow, scheme = _schema()
    snap = _snapshot()
    payload = _search_payload()
    statuses = common.parse_workflow_status_map(payload["statuses"])
    snap.workflows["Tracker Workflow"] = common.parse_workflow(payload["values"][0], statuses)
    snap.workflow_schemes["10000"] = WorkflowSchemeSnapshot(
        id="10000", name="Tracker WFS", default_workflow="Tracker Workflow"
    )
    state = _state()
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=state)
    changes = result.changes
    assert any(isinstance(c, AdoptWorkflow) for c in changes)
    assert any(isinstance(c, AdoptWorkflowScheme) for c in changes)
    assert not any(isinstance(c, (CreateWorkflow, CreateWorkflowScheme)) for c in changes)


def test_diff_update_carries_declared_and_preserves_live():
    project_cls, workflow, scheme = _schema()
    snap = _snapshot()
    payload = _search_payload()
    statuses = common.parse_workflow_status_map(payload["statuses"])
    live = common.parse_workflow(payload["values"][0], statuses)
    snap.workflows[live.name] = live
    state = _state()
    state.projects["TRK"] = ProjectMapping(id="10010", key="TRK")
    state.workflows["tracker_flow"] = WorkflowMapping(
        id=live.id, name=live.name, version_id=live.version_id, version_number=live.version_number
    )
    state.workflow_schemes["tracker_wfs"] = WorkflowSchemeMapping(id="10000")
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=state)
    update = next((c for c in result.changes if isinstance(c, UpdateWorkflow)), None)
    assert update is not None  # declared Close/Start/Create not all on live
    assert any(t.name == "Close" for t in update.transitions)
    assert update.version_id == "v-1"


def test_diff_orphan_deletes_behind_allow_delete():
    project_cls, _workflow, _scheme = _schema()
    state = _state()
    state.workflows["orphan_flow"] = WorkflowMapping(id="dead-beef", name="Orphan")
    state.workflow_schemes["orphan_wfs"] = WorkflowSchemeMapping(id="20000")
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=state, allow_delete=False)
    assert not any(isinstance(c, (DeleteWorkflow,)) for c in result.changes)
    assert any("orphan_flow" in w for w in result.warnings)
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=state, allow_delete=True)
    assert any(isinstance(c, DeleteWorkflow) for c in result.changes)


# ── Emit ─────────────────────────────────────────────────────────────
def test_render_create_workflow_roundtrips_through_compile():
    project_cls, _workflow, _scheme = _schema()
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=_state())
    workflow_changes = [
        c for c in result.changes if isinstance(c, (CreateWorkflow, CreateWorkflowScheme, SetProjectWorkflowScheme))
    ]
    rendered = "\n".join(_render_one(c) for c in workflow_changes)
    compile(textwrap.dedent(f"async def upgrade():\n{rendered}"), "<migration>", "exec")
    assert "op.create_workflow(" in rendered
    assert "op.create_workflow_scheme(" in rendered
    assert "op.set_project_workflow_scheme(" in rendered


# ── Ops ──────────────────────────────────────────────────────────────
def _engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


async def _run_in_ctx(engine: Engine, state: StateFile, body: Any) -> Any:
    ctx = MigrationContext(engine=engine, state=state, direction="upgrade")
    token = set_context(ctx)
    try:
        return await body()
    finally:
        reset_context(token)


def _stub_restrict_facts(mock: respx.MockRouter) -> None:
    """Endpoints the restrict-holder resolution and guards read."""
    stub_workflows(mock, CLOUD_ROOT)
    mock.get(f"{CLOUD_ROOT}/myself").mock(return_value=httpx.Response(200, json={"accountId": "acct-svc"}))
    mock.get(f"{CLOUD_ROOT}/user").mock(
        return_value=httpx.Response(200, json={"groups": {"size": 1, "items": [{"name": "jira-users"}]}})
    )
    mock.get(f"{CLOUD_ROOT}/role").mock(
        return_value=httpx.Response(200, json=paginated([{"id": "10360", "name": "Administrators"}]))
    )
    mock.get(f"{CLOUD_ROOT}/group/bulk").mock(
        return_value=httpx.Response(200, json={"values": [{"groupId": "g-ops", "name": "ops"}], "isLast": True})
    )
    mock.get(f"{CLOUD_ROOT}/statuses/byNames").mock(return_value=httpx.Response(200, json=[]))


_CREATE_RESPONSE = {
    "statuses": [
        {"id": "10001", "name": "Open", "statusCategory": "TODO", "statusReference": "stint-open"},
        {"id": "10002", "name": "In Progress", "statusCategory": "IN_PROGRESS", "statusReference": "stint-in-progress"},
        {"id": "10003", "name": "Done", "statusCategory": "DONE", "statusReference": "stint-done"},
    ],
    "workflows": [
        {
            "id": "wf-uuid-1",
            "name": "Tracker Workflow",
            "version": {"id": "ver-1", "versionNumber": 0},
        }
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_create_workflow_builds_payload_and_records_state():
    mock = respx.mock
    _stub_restrict_facts(mock)
    create_route = mock.post(f"{CLOUD_ROOT}/workflows/create").mock(
        return_value=httpx.Response(200, json=_CREATE_RESPONSE)
    )
    engine = _engine()
    state = _state()

    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_workflow(
                alias="tracker_flow",
                name="Tracker Workflow",
                statuses=[
                    {"name": "Open", "category": "new"},
                    {"name": "In Progress", "category": "indeterminate"},
                    {"name": "Done", "category": "done"},
                ],
                initial="Open",
                transitions=[
                    {"name": "Start", "from_": ["Open"], "to": "In Progress"},
                    {
                        "name": "Close",
                        "from_": ["In Progress"],
                        "to": "Done",
                        "restrict_to": [{"holder_type": "projectRole", "holder_parameter": "Administrators"}],
                    },
                ],
            ),
        )

    body = create_route.calls.last.request.read()
    import json as _json

    payload = _json.loads(body)
    assert payload["scope"] == {"type": "GLOBAL"}
    (wf,) = payload["workflows"]
    assert wf["name"] == "Tracker Workflow"
    references = {s["name"]: s["statusReference"] for s in payload["statuses"]}
    (create_transition,) = [t for t in wf["transitions"] if t["type"] == "INITIAL"]
    assert create_transition["name"] == "Create"
    assert create_transition["toStatusReference"] == references["Open"]
    (close,) = [t for t in wf["transitions"] if t["name"] == "Close"]
    assert close["type"] == "DIRECTED"
    assert close["links"] == [{"fromStatusReference": references["In Progress"]}]
    assert close["conditions"]["operation"] == "ANY"
    assert close["conditions"]["conditions"] == [
        {"ruleKey": "system:restrict-issue-transition", "parameters": {"roleIds": "10360"}}
    ]
    mapping = state.workflows["tracker_flow"]
    assert mapping.id == "wf-uuid-1"
    assert mapping.version_id == "ver-1"
    assert mapping.status_ids == {"Open": "10001", "In Progress": "10002", "Done": "10003"}


@pytest.mark.asyncio
@respx.mock
async def test_create_workflow_reuses_existing_statuses():
    mock = respx.mock
    _stub_restrict_facts(mock)
    # byNames resolves Open/Done as existing global statuses.
    mock.get(f"{CLOUD_ROOT}/statuses/byNames").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "9001", "name": "Open", "statusCategory": "TODO"},
                {"id": "9003", "name": "Done", "statusCategory": "DONE"},
            ],
        )
    )
    create_route = mock.post(f"{CLOUD_ROOT}/workflows/create").mock(
        return_value=httpx.Response(
            200,
            json={
                "statuses": [
                    {
                        "id": "10002",
                        "name": "In Progress",
                        "statusCategory": "IN_PROGRESS",
                        "statusReference": "stint-in-progress",
                    },
                    {"id": "9001", "name": "Open", "statusCategory": "TODO", "statusReference": "9001"},
                    {"id": "9003", "name": "Done", "statusCategory": "DONE", "statusReference": "9003"},
                ],
                "workflows": _CREATE_RESPONSE["workflows"],
            },
        )
    )
    engine = _engine()
    state = _state()

    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_workflow(
                alias="tracker_flow",
                name="Tracker Workflow",
                statuses=[{"name": "Open", "category": "new"}, {"name": "Done", "category": "done"}],
                initial="Open",
                transitions=[{"name": "Done", "from_": ["Open"], "to": "Done"}],
            ),
        )

    import json as _json

    payload = _json.loads(create_route.calls.last.request.read())
    status_by_name = {s["name"]: s for s in payload["statuses"]}
    assert status_by_name["Open"]["id"] == "9001"  # reused, not re-created
    assert "In Progress" not in status_by_name
    assert state.workflows["tracker_flow"].status_ids["Open"] == "9001"
    assert state.workflows["tracker_flow"].status_ids["Done"] == "9003"


@pytest.mark.asyncio
@respx.mock
async def test_create_workflow_refuses_name_collision():
    mock = respx.mock
    _stub_restrict_facts(mock)
    mock.get(f"{CLOUD_ROOT}/workflows/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": [{"id": "existing", "name": "Tracker Workflow", "scope": {"type": "GLOBAL"}}],
                "statuses": [],
                "isLast": True,
            },
        )
    )
    engine = _engine()
    state = _state()
    async with engine:
        with pytest.raises(ConfigurationError, match="already exists"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_workflow(
                    alias="tracker_flow",
                    name="Tracker Workflow",
                    statuses=[{"name": "Open", "category": "new"}],
                    initial="Open",
                    transitions=[],
                ),
            )


@pytest.mark.asyncio
@respx.mock
async def test_adopt_workflow_records_without_rewrite():
    mock = respx.mock
    _stub_restrict_facts(mock)
    mock.get(f"{CLOUD_ROOT}/workflows/search").mock(return_value=httpx.Response(200, json=_search_payload()))
    engine = _engine()
    state = _state()
    async with engine:
        await _run_in_ctx(engine, state, lambda: op.adopt_workflow(alias="tracker_flow", name="Tracker Workflow"))
    mapping = state.workflows["tracker_flow"]
    assert mapping.id == "c5ef565c-1b1e-427e-bc3b-e677b0dc027c"
    assert mapping.version_id == "v-1" and mapping.version_number == 3
    assert mapping.status_ids == {"Open": "10001", "Done": "10002"}
    # Nothing was written.
    assert not [c for c in mock.calls if c.request.method == "POST"]


def _stub_scheme_facts(mock: respx.MockRouter, *, project_in_state: bool = True) -> None:
    _stub_restrict_facts(mock)
    stub_project_roles_and_permission_schemes(mock, CLOUD_ROOT)
    mock.get(f"{CLOUD_ROOT}/mypermissions").mock(
        return_value=httpx.Response(
            200,
            json={
                "permissions": {
                    key: {"havePermission": True}
                    for key in ("ADMINISTER_PROJECTS", "BROWSE_PROJECTS", "TRANSITION_ISSUES")
                }
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/project/10010").mock(
        return_value=httpx.Response(200, json={"id": "10010", "lead": {"accountId": "acct-svc"}})
    )
    mock.get(f"{CLOUD_ROOT}/project/10010/role").mock(
        return_value=httpx.Response(
            200,
            json={
                "Administrators": f"{CLOUD_ROOT}/project/10010/role/10360",
                "Viewers": f"{CLOUD_ROOT}/project/10010/role/10361",
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/project/10010/role/10360").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "10360",
                "name": "Administrators",
                "actors": [{"id": "a1", "type": "atlassian-user-role-actor", "actorUser": {"accountId": "acct-svc"}}],
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/project/10010/role/10361").mock(
        return_value=httpx.Response(200, json={"id": "10361", "name": "Viewers", "actors": []})
    )
    mock.post(f"{CLOUD_ROOT}/workflowscheme/update/mappings").mock(
        return_value=httpx.Response(
            200,
            json={
                "statusMappingsByIssueTypes": [
                    {"issueTypeId": "10001", "statusIds": ["1"]},
                ],
                "statuses": [{"id": "1", "name": "Legacy Status", "category": "TODO"}],
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/statuses/byNames").mock(
        return_value=httpx.Response(200, json=[{"id": "1", "name": "Legacy Status", "statusCategory": "TODO"}])
    )
    if project_in_state:
        mock.put(f"{CLOUD_ROOT}/workflowscheme/project").mock(return_value=httpx.Response(204))


@pytest.mark.asyncio
@respx.mock
async def test_create_workflow_scheme_records_mappings():
    mock = respx.mock
    _stub_restrict_facts(mock)
    create_route = mock.post(f"{CLOUD_ROOT}/workflowscheme").mock(
        return_value=httpx.Response(201, json={"id": 10000, "name": "Tracker WFS"})
    )
    engine = _engine()
    state = _state()
    state.workflows["tracker_flow"] = WorkflowMapping(id="wf-uuid-1", name="Tracker Workflow")
    from stint.state.file import SimpleMapping

    state.issuetypes["bug"] = SimpleMapping(id="10001")

    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_workflow_scheme(
                alias="tracker_wfs",
                name="Tracker WFS",
                default_workflow="tracker_flow",
                mappings=[{"issuetype": "bug", "workflow": "tracker_flow"}],
            ),
        )

    import json as _json

    payload = _json.loads(create_route.calls.last.request.read())
    assert payload["defaultWorkflow"] == "Tracker Workflow"
    assert payload["issueTypeMappings"] == {"10001": "Tracker Workflow"}
    scheme = state.workflow_schemes["tracker_wfs"]
    assert scheme.id == "10000"
    assert scheme.workflows == {"tracker_flow": "wf-uuid-1"}
    assert scheme.issue_type_ids == {"bug": "10001"}
    assert scheme.mappings == {"bug": "tracker_flow"}
    assert scheme.default_workflow == "tracker_flow"


@pytest.mark.asyncio
@respx.mock
async def test_set_project_workflow_scheme_associates_empty_project():
    mock = respx.mock
    _stub_scheme_facts(mock)
    associate = mock.put(f"{CLOUD_ROOT}/workflowscheme/project").mock(return_value=httpx.Response(204))
    engine = _engine()
    state = _state()
    state.workflow_schemes["tracker_wfs"] = WorkflowSchemeMapping(id="10000")

    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_workflow_scheme(project_alias="TRK", scheme_alias="tracker_wfs", live=False),
        )
    assert associate.called
    assert state.projects["TRK"].workflow_scheme_id == "10000"


@pytest.mark.asyncio
@respx.mock
async def test_live_switch_refuses_unmapped_statuses():
    mock = respx.mock
    _stub_scheme_facts(mock)
    state = _state()
    state.workflow_schemes["tracker_wfs"] = WorkflowSchemeMapping(
        id="10000", workflows={"tracker_flow": "wf-uuid-1"}, default_workflow="tracker_flow"
    )
    state.workflows["tracker_flow"] = WorkflowMapping(
        id="wf-uuid-1", name="Tracker Workflow", status_ids={"Open": "10001", "Done": "10003"}
    )
    engine = _engine()
    async with engine:
        with pytest.raises(ConfigurationError, match="status_mappings"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.set_project_workflow_scheme(
                    project_alias="TRK",
                    scheme_alias="tracker_wfs",
                    live=True,
                    status_mappings={},
                ),
            )


def _stub_switch_flow(mock: respx.MockRouter) -> respx.Route:
    """Live-switch flow: current scheme + required mappings + task polling."""
    _stub_scheme_facts(mock)
    # Current (old) scheme: default = the live workflow read below.
    mock.get(f"{CLOUD_ROOT}/workflowscheme/20000").mock(
        return_value=httpx.Response(
            200,
            json={"id": 20000, "name": "Old", "defaultWorkflow": "Legacy Flow", "issueTypeMappings": {}},
        )
    )
    mock.get(f"{CLOUD_ROOT}/workflowscheme/10000").mock(
        return_value=httpx.Response(
            200,
            json={"id": 10000, "name": "Tracker WFS", "defaultWorkflow": "Tracker Workflow", "issueTypeMappings": {}},
        )
    )

    # Workflow reads: the search endpoint serves per-queryName payloads.
    def _search_response(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("queryString", "")
        if name == "Legacy Flow":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "id": "wf-old",
                            "name": "Legacy Flow",
                            "scope": {"type": "GLOBAL"},
                            "version": {"id": "v-old", "versionNumber": 1},
                            "statuses": [{"statusReference": "1"}],
                            "transitions": [
                                {"id": "1", "name": "Create", "type": "INITIAL", "toStatusReference": "1", "links": []},
                                {
                                    "id": "2",
                                    "name": "Close",
                                    "type": "DIRECTED",
                                    "toStatusReference": "1",
                                    "links": [{"fromStatusReference": "1"}],
                                },
                            ],
                        }
                    ],
                    "statuses": [
                        {"id": "1", "name": "Legacy Status", "statusCategory": "TODO", "scope": {"type": "GLOBAL"}}
                    ],
                    "isLast": True,
                },
            )
        return httpx.Response(200, json=_search_payload())

    mock.get(f"{CLOUD_ROOT}/workflows/search").mock(side_effect=_search_response)
    switch = mock.post(f"{CLOUD_ROOT}/workflowscheme/project/switch").mock(
        return_value=httpx.Response(303, headers={"Location": f"{CLOUD_ROOT}/task/77"})
    )
    mock.get(f"{CLOUD_ROOT}/task/77").mock(
        return_value=httpx.Response(200, json={"id": "77", "status": "COMPLETE", "progress": 100})
    )
    return switch


@pytest.mark.asyncio
@respx.mock
async def test_live_switch_polls_task_and_records_binding():
    mock = respx.mock
    switch = _stub_switch_flow(mock)
    state = _state()
    state.workflow_schemes["tracker_wfs"] = WorkflowSchemeMapping(
        id="10000", workflows={"tracker_flow": "wf-uuid-1"}, default_workflow="tracker_flow"
    )
    state.workflows["tracker_flow"] = WorkflowMapping(
        id="wf-uuid-1", name="Tracker Workflow", status_ids={"Open": "10001", "Done": "10003"}
    )
    engine = _engine()
    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_workflow_scheme(
                project_alias="TRK",
                scheme_alias="tracker_wfs",
                live=True,
                status_mappings={"Legacy Status": "Done"},
            ),
        )
    assert switch.called
    assert state.projects["TRK"].workflow_scheme_id == "10000"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.asyncio
@respx.mock
async def test_live_switch_guard_refuses_lost_transition():
    """The account can use Close on the current scheme (unrestricted); the
    target's live Close is restricted to a group it is not in → refused."""
    mock = respx.mock
    switch = _stub_switch_flow(mock)

    # Target workflow read: Close restricted to a group the account lacks
    # (group/bulk only knows "ops"; the account's group is "jira-users").
    def _search_response(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("queryString", "")
        if name == "Legacy Flow":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "id": "wf-old",
                            "name": "Legacy Flow",
                            "scope": {"type": "GLOBAL"},
                            "version": {"id": "v-old", "versionNumber": 1},
                            "statuses": [{"statusReference": "1"}],
                            "transitions": [
                                {"id": "1", "name": "Create", "type": "INITIAL", "toStatusReference": "1", "links": []},
                                {
                                    "id": "2",
                                    "name": "Close",
                                    "type": "DIRECTED",
                                    "toStatusReference": "1",
                                    "links": [{"fromStatusReference": "1"}],
                                },
                            ],
                        }
                    ],
                    "statuses": [
                        {"id": "1", "name": "Legacy Status", "statusCategory": "TODO", "scope": {"type": "GLOBAL"}}
                    ],
                    "isLast": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "values": [
                    {
                        "id": "wf-uuid-1",
                        "name": "Tracker Workflow",
                        "scope": {"type": "GLOBAL"},
                        "version": {"id": "v-1", "versionNumber": 3},
                        "statuses": [{"statusReference": "10001"}, {"statusReference": "10003"}],
                        "transitions": [
                            {"id": "1", "name": "Create", "type": "INITIAL", "toStatusReference": "10001", "links": []},
                            {
                                "id": "31",
                                "name": "Close",
                                "type": "DIRECTED",
                                "toStatusReference": "10003",
                                "links": [{"fromStatusReference": "10001"}],
                                "conditions": {
                                    "operation": "ANY",
                                    "conditions": [
                                        {
                                            "ruleKey": "system:restrict-issue-transition",
                                            "parameters": {"groupIds": "g-other"},
                                        }
                                    ],
                                },
                            },
                        ],
                    }
                ],
                "statuses": [
                    {"id": "10001", "name": "Open", "statusCategory": "TODO", "scope": {"type": "GLOBAL"}},
                    {"id": "10003", "name": "Done", "statusCategory": "DONE", "scope": {"type": "GLOBAL"}},
                ],
                "isLast": True,
            },
        )

    mock.get(f"{CLOUD_ROOT}/workflows/search").mock(side_effect=_search_response)
    state = _state()
    state.projects["TRK"] = ProjectMapping(id="10010", key="TRK", workflow_scheme_id="20000")
    state.workflow_schemes["tracker_wfs"] = WorkflowSchemeMapping(
        id="10000", workflows={"tracker_flow": "wf-uuid-1"}, default_workflow="tracker_flow"
    )
    state.workflows["tracker_flow"] = WorkflowMapping(
        id="wf-uuid-1", name="Tracker Workflow", status_ids={"Open": "10001", "Done": "10003"}
    )
    engine = _engine()
    async with engine:
        with pytest.raises(Exception, match="Close"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.set_project_workflow_scheme(
                    project_alias="TRK",
                    scheme_alias="tracker_wfs",
                    live=True,
                    status_mappings={"Legacy Status": "Done"},
                ),
            )
    # Nothing was switched.
    assert not switch.called


# ── Lockout evaluators ───────────────────────────────────────────────
def test_restrict_rule_parameters_merges_and_ignores_other_rules():
    conditions = {
        "operation": "ALL",
        "conditions": [
            {"ruleKey": "system:restrict-issue-transition", "parameters": {"roleIds": "1,2"}},
            {"ruleKey": "system:check-field-value", "parameters": {"fieldId": "5"}},
        ],
        "conditionGroups": [
            {
                "operation": "ANY",
                "conditions": [{"ruleKey": "system:restrict-issue-transition", "parameters": {"roleIds": "2,3"}}],
            }
        ],
    }
    assert restrict_rule_parameters(conditions) == {"roleIds": "1,2,3"}
    assert restrict_rule_parameters(None) is None
    assert restrict_rule_parameters({"operation": "ANY"}) is None


def test_account_can_transition_and_lost_transitions():
    params = {"roleIds": "10360", "groupIds": "g-ops"}
    assert account_can_transition(params, account_id="a", account_role_ids=[], account_group_ids=[]) is False
    assert account_can_transition(params, account_id="a", account_role_ids=["10360"], account_group_ids=[]) is True
    assert account_can_transition(params, account_id="a", account_role_ids=[], account_group_ids=["g-ops"]) is True
    assert account_can_transition(None, account_id="a", account_role_ids=[], account_group_ids=[]) is True
    # permissionKeys need the held set.
    perms = {"permissionKeys": "ADMINISTER_PROJECTS"}
    assert account_can_transition(perms, account_id="a", account_role_ids=[], account_group_ids=[]) is False
    assert (
        account_can_transition(
            perms,
            account_id="a",
            account_role_ids=[],
            account_group_ids=[],
            held_permission_keys=["ADMINISTER_PROJECTS"],
        )
        is True
    )
    lost = lost_transitions(
        {"wf:Close": None},
        {"wf:Close": params},
        account_id="a",
        account_role_ids=[],
        account_group_ids=[],
    )
    assert lost == ("wf:Close",)
    # Deleting a transition the account used counts as lost.
    assert lost_transitions({"wf:Close": None}, {}, account_id="a", account_role_ids=[], account_group_ids=[]) == (
        "wf:Close",
    )
    # No violation when the account never had access.
    assert (
        lost_transitions(
            {"wf:X": {"roleIds": "9"}}, {"wf:X": None}, account_id="a", account_role_ids=[], account_group_ids=[]
        )
        == ()
    )
