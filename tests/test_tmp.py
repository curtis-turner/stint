"""M7 partial: team-managed project support.

What's in scope here:

- ``ProjectMapping`` carries ``style`` and ``key``; YAML round-trip preserves both.
- ``op.create_project`` records ``style`` in state and defaults the
  ``project_template_key`` for team-managed creates.
- ``set_project_*_scheme`` ops refuse to run on team-managed projects and
  surface a Jira UI deep link via ``UnsupportedTMPOpError``.
- Stamp captures ``style`` from Jira and back-fills it on pre-M7 state files.
- Diff warns (no Change emitted) when schema style and state style disagree.
- ``DesiredProject`` reads ``__style__`` from the Project class.
- Project metaclass enforces style/scheme combinations.
"""

import sys
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from stint import PATAuth, StateFile, create_engine, op
from stint.autogen.desired import build_desired_snapshot
from stint.autogen.diff import UpdateProject, diff
from stint.autogen.stamp import stamp
from stint.dialects.jira.tmp.dialect import TmpDialect
from stint.dialects.jira.tmp.models import TmpProjectContext
from stint.dialects.jira.tmp.ops import TmpApplyContext, tmp_upsert_automation_rule
from stint.dialects.jira.tmp.reconcile import UpsertAutomationRule
from stint.dialects.jira.tmp.state import TmpState
from stint.engine import Engine
from stint.exceptions import ConfigurationError, UnsupportedTMPOpError
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.registry import registry
from stint.state.file import AutomationRuleMapping, ProjectMapping, SimpleMapping
from stint.state.snapshot import (
    AutomationRuleSnapshot,
    ProjectSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"
CLOUD_ID_TMP = "1a2b3c4d-0000-4000-8000-000000000009"
AUTOMATION_ROOT = f"https://api.atlassian.com/automation/public/jira/{CLOUD_ID_TMP}/rest/v1"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


async def _run_in_ctx(
    engine: Engine,
    state: StateFile,
    body: Callable[[], Awaitable[object]],
) -> None:
    ctx = MigrationContext(engine=engine, state=state, direction="upgrade")
    token = set_context(ctx)
    try:
        await body()
    finally:
        reset_context(token)


def _empty_snapshot() -> Snapshot:
    return Snapshot(
        server_info=ServerInfoSnapshot(
            deployment_type="Cloud",
            version="9",
            base_url="x",
        )
    )


# ── ProjectMapping YAML round-trip ───────────────────────────────────
def test_project_mapping_yaml_default_style_omits_field():
    """company-managed is the default; YAML should stay tidy and not write it."""
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["BUG"] = ProjectMapping(id="p-1", key="BUG")
    text = state.to_yaml()
    # style default is implicit
    assert "style:" not in text
    assert "key: BUG" in text


def test_project_mapping_yaml_team_managed_writes_style():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["BUG"] = ProjectMapping(id="p-1", style="team-managed", key="BUG")
    text = state.to_yaml()
    assert "style: team-managed" in text


def test_project_mapping_yaml_round_trip_preserves_style_and_key():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["cmp"] = ProjectMapping(id="p-1", key="CMP")
    state.projects["tmp"] = ProjectMapping(id="p-2", style="team-managed", key="TMP")
    loaded = StateFile.from_yaml(state.to_yaml())
    assert loaded.projects["cmp"].style == "company-managed"
    assert loaded.projects["cmp"].key == "CMP"
    assert loaded.projects["tmp"].style == "team-managed"
    assert loaded.projects["tmp"].key == "TMP"


def test_project_mapping_from_dict_defaults_style():
    """A state file written before M7 has no style; load must default it."""
    pre_m7 = {"id": "p-1"}
    pm = ProjectMapping.from_dict(pre_m7)
    assert pm.style == "company-managed"
    assert pm.key == ""


# ── op.create_project records style ──────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_project_records_default_style():
    respx.post(f"{CLOUD_ROOT}/project").mock(
        return_value=httpx.Response(201, json={"id": "p-1"}),
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_project(
                alias="bug_tracker",
                key="BUG",
                name="Bug Tracker",
                project_type_key="software",
                lead="jdoe",
            ),
        )
    finally:
        await engine.close()
    assert state.projects["bug_tracker"].style == "company-managed"
    assert state.projects["bug_tracker"].key == "BUG"


@pytest.mark.asyncio
@respx.mock
async def test_create_project_team_managed_defaults_template():
    """TMP creates need a projectTemplateKey; create_project picks the
    next-gen Kanban template when the caller omits it."""
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "1001",
                "deploymentType": "Cloud",
            },
        )
    )
    route = respx.post(f"{CLOUD_ROOT}/project").mock(
        return_value=httpx.Response(201, json={"id": "p-1"}),
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_project(
                alias="growth",
                key="GRW",
                name="Growth",
                project_type_key="software",
                lead="acc-1",
                style="team-managed",
            ),
        )
    finally:
        await engine.close()
    body = route.calls.last.request.read().decode()
    assert "gh-simplified-agility-kanban" in body
    assert state.projects["growth"].style == "team-managed"


@pytest.mark.asyncio
async def test_create_project_rejects_unknown_style():
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError, match="unknown style"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_project(
                    alias="x",
                    key="X",
                    name="X",
                    project_type_key="software",
                    lead="jdoe",
                    style="next-gen",  # snapshot vocabulary, not stint's
                ),
            )
    finally:
        await engine.close()


# ── TMP guard: set_project_*_scheme refuses team-managed ─────────────
@pytest.mark.asyncio
async def test_set_project_issuetype_screen_scheme_refuses_tmp():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["growth"] = ProjectMapping(
        id="p-1",
        style="team-managed",
        key="GRW",
    )
    state.issuetype_screen_schemes["grw_itss"] = SimpleMapping(id="itss-1")
    engine = _cloud_engine()
    try:
        with pytest.raises(UnsupportedTMPOpError) as excinfo:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.set_project_issuetype_screen_scheme(
                    project_alias="growth",
                    scheme_alias="grw_itss",
                ),
            )
    finally:
        await engine.close()
    msg = str(excinfo.value)
    assert "team-managed" in msg
    assert f"{BASE}/jira/software/projects/GRW/settings/details" in msg


@pytest.mark.asyncio
async def test_set_project_field_configuration_scheme_refuses_tmp():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["growth"] = ProjectMapping(
        id="p-1",
        style="team-managed",
        key="GRW",
    )
    state.field_configuration_schemes["grw_fcs"] = SimpleMapping(id="fcs-1")
    engine = _cloud_engine()
    try:
        with pytest.raises(UnsupportedTMPOpError) as excinfo:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.set_project_field_configuration_scheme(
                    project_alias="growth",
                    scheme_alias="grw_fcs",
                ),
            )
    finally:
        await engine.close()
    assert f"{BASE}/jira/software/projects/GRW/settings/details" in str(excinfo.value)


@pytest.mark.asyncio
@respx.mock
async def test_set_project_issuetype_screen_scheme_allowed_on_cmp():
    """Sanity check: CMP project doesn't trigger the TMP guard."""
    respx.put(f"{CLOUD_ROOT}/issuetypescreenscheme/project").mock(
        return_value=httpx.Response(204),
    )
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["plat"] = ProjectMapping(id="p-1", key="PLAT")
    state.issuetype_screen_schemes["plat_itss"] = SimpleMapping(id="itss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_issuetype_screen_scheme(
                project_alias="plat",
                scheme_alias="plat_itss",
            ),
        )
    finally:
        await engine.close()


# ── Stamp captures style ─────────────────────────────────────────────
def test_stamp_classifies_next_gen_as_team_managed():
    from stint import IssueType, Project

    class _Task(IssueType):
        __alias__ = "task"
        summary: str

    class _Growth(Project):
        __key__ = "GRW"
        __style__ = "team-managed"
        __issuetypes__ = [_Task]

    state = StateFile(env="dev", jira_url=BASE)
    snap = _empty_snapshot()
    snap.projects["GRW"] = ProjectSnapshot(
        id="p-1",
        key="GRW",
        name="Growth",
        style="next-gen",
    )
    stamp(state, snap)
    assert state.projects["GRW"].style == "team-managed"
    assert state.projects["GRW"].key == "GRW"


def test_stamp_classifies_classic_as_company_managed():
    from stint import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __issuetypes__ = [_Bug]

    state = StateFile(env="dev", jira_url=BASE)
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="Platform",
        style="classic",
    )
    stamp(state, snap)
    assert state.projects["PLAT"].style == "company-managed"


def test_stamp_backfills_style_on_pre_m7_state():
    """A state file from before M7 records id only, defaulting style to CMP.
    If Jira actually has TMP, the next stamp should fix it."""
    from stint import IssueType, Project

    class _Task(IssueType):
        __alias__ = "task"
        summary: str

    class _Growth(Project):
        __key__ = "GRW"
        __style__ = "team-managed"
        __issuetypes__ = [_Task]

    state = StateFile(env="dev", jira_url=BASE)
    state.projects["GRW"] = ProjectMapping(id="p-1")  # no style, no key
    snap = _empty_snapshot()
    snap.projects["GRW"] = ProjectSnapshot(
        id="p-1",
        key="GRW",
        name="Growth",
        style="next-gen",
    )
    stamp(state, snap)
    assert state.projects["GRW"].style == "team-managed"
    assert state.projects["GRW"].key == "GRW"


def test_stamp_skips_id_divergence_without_overwriting_style():
    from stint import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __issuetypes__ = [_Bug]

    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(
        id="p-OLD",
        style="team-managed",
        key="PLAT",
    )
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-NEW",
        key="PLAT",
        name="Platform",
        style="classic",
    )
    report = stamp(state, snap)
    assert state.projects["PLAT"].id == "p-OLD"
    assert state.projects["PLAT"].style == "team-managed"
    assert any(entry[0] == "project" and entry[1] == "PLAT" for entry in report.skipped)


# ── Diff warns on style mismatch ─────────────────────────────────────
def test_diff_warns_on_style_mismatch():
    from stint import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __style__ = "team-managed"  # schema says TMP …
        __issuetypes__ = [_Bug]

    desired = build_desired_snapshot()
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(
        id="p-1",
        style="company-managed",
        key="PLAT",  # … but state has CMP
    )
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="_Plat",
        style="classic",
    )
    result = diff(desired=desired, snapshot=snap, state=state, allow_delete=False)
    assert any("team-managed" in w and "company-managed" in w and "PLAT" in w for w in result.warnings)
    # Style alone produces no UpdateProject change — Jira has no REST conversion.
    updates = [c for c in result.changes if isinstance(c, UpdateProject)]
    assert updates == []


def test_diff_quiet_when_styles_match():
    from stint import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __issuetypes__ = [_Bug]

    desired = build_desired_snapshot()
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(
        id="p-1",
        style="company-managed",
        key="PLAT",
    )
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="_Plat",
        style="classic",
    )
    result = diff(desired=desired, snapshot=snap, state=state, allow_delete=False)
    assert not any("style" in w for w in result.warnings)


# ── DesiredProject + Project metaclass ───────────────────────────────
def test_desired_project_reads_style():
    from stint import IssueType, Project

    class _Task(IssueType):
        __alias__ = "task"
        summary: str

    class _Growth(Project):
        __key__ = "GRW"
        __style__ = "team-managed"
        __issuetypes__ = [_Task]

    desired = build_desired_snapshot()
    assert desired.projects["GRW"].style == "team-managed"


def test_project_metaclass_rejects_unknown_style():
    from stint import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    with pytest.raises(ConfigurationError, match="invalid __style__"):

        class _Bad(Project):
            __key__ = "BAD"
            __style__ = "hybrid"
            __issuetypes__ = [_Bug]


def test_project_metaclass_rejects_tmp_with_screen_scheme():
    from stint import IssueType, Project, Screen, ScreenScheme

    s = Screen(alias="s", name="S", fields=["Summary"])
    ss = ScreenScheme(alias="ss", name="SS", create=s, edit=s, view=s)

    class _Bug(IssueType):
        __alias__ = "bug"
        __screen_scheme__ = ss
        summary: str

    with pytest.raises(ConfigurationError, match="team-managed.*ScreenScheme"):

        class _Bad(Project):
            __key__ = "BAD"
            __style__ = "team-managed"
            __issuetypes__ = [_Bug]


def test_project_metaclass_rejects_tmp_with_field_configuration():
    from stint import FieldConfiguration, IssueType, Project

    fc = FieldConfiguration(alias="fc", name="FC", required=["Summary"])

    class _Bug(IssueType):
        __alias__ = "bug"
        __field_configuration__ = fc
        summary: str

    with pytest.raises(ConfigurationError, match="team-managed.*FieldConfiguration"):

        class _Bad(Project):
            __key__ = "BAD"
            __style__ = "team-managed"
            __issuetypes__ = [_Bug]


# ── Automation rules on team-managed projects ─────────────────────────
# Rules are tenant objects: the scope ARI takes a team-managed project id
# like any other, so the bound scheme is reconciled by the TMP apply path
# with the same context-free cores the company-managed ops use.
def _tmp_guard_rule(**overrides: Any) -> Any:
    from stint.schema.automation import ControlFieldGuard
    from stint.schema.permissions import ProjectRole  # noqa: F401  (role writers are rejected for TMP)
    from stint.schema.roles import User

    kwargs: dict[str, Any] = {
        "alias": "vuln_control_guard",
        "name": "VULN: revert control-field edits",
        "fields": [_tmp_severity_field()],
        # The authenticated account (acct-me = me@example.com in the mocked
        # tenant) must be an allowed writer — stint refuses a guard that
        # would revert the service's own writes.
        "allowed_writers": [User("me@example.com")],
        "on_violation": ["revert", "comment"],
    }
    kwargs.update(overrides)
    return ControlFieldGuard(**kwargs)


def _tmp_severity_field() -> Any:
    from stint import CustomField, SelectField

    return CustomField(alias="vuln_severity", name="Severity", type=SelectField, options=["Low", "High"])


def _tmp_schema_project(**guard_overrides: Any) -> Any:
    """A team-managed Project with the guard bound (fresh classes per test)."""
    from stint import IssueType, Project
    from stint.schema.automation import AutomationRuleScheme

    guard = _tmp_guard_rule(**guard_overrides)
    scheme = AutomationRuleScheme(alias="vuln_rules", name="VULN Automation", rules=[guard])

    class Vuln(IssueType):
        __alias__ = "vuln"

    class VulnProject(Project):
        __key__ = "VULN"
        __style__ = "team-managed"
        __issuetypes__ = [Vuln]
        __automation_rule_scheme__ = scheme

    return VulnProject


def test_tmp_desired_carries_automation_rules():
    from stint.dialects.jira.tmp.desired import build_tmp_desired

    project_cls = _tmp_schema_project()
    desired = build_tmp_desired(project_cls)
    (record,) = desired.automation_rules.values()
    assert record.is_guard is True
    assert record.scope_projects == ("VULN",)
    assert record.payload["components"]  # trigger + condition + edit + comment
    assert ("tmp_severity", "field", "tmp_severity") in record.refs or (
        "vuln_severity",
        "field",
        "vuln_severity",
    ) in record.refs
    # The field ref resolves through the TMP field map at apply time, so the
    # ref's declared value is the field alias the TmpState records.
    field_refs = [r for r in record.refs if r[1] == "field"]
    assert field_refs, "the guard payload must reference the declared field"


def test_plan_tmp_automation_create_adopt_update_delete():
    import json

    from stint.autogen.desired import automation_rule_record
    from stint.dialects.jira.tmp.reconcile import (
        DeleteAutomationRule,
        UpsertAutomationRule,
        plan_tmp_automation,
    )

    project_cls = _tmp_schema_project()
    from stint.dialects.jira.tmp.desired import build_tmp_desired

    desired = build_tmp_desired(project_cls)
    rules = desired.automation_rules
    state = StateFile(env="devel", jira_url=BASE)

    # Nothing in state, nothing in Jira: create.
    changes, warnings = plan_tmp_automation(rules, {}, state, {})
    assert [(c.alias, c.action) for c in changes if isinstance(c, UpsertAutomationRule)] == [
        ("vuln_control_guard", "create")
    ]
    assert not warnings

    # A rule with the declared name already exists: adopt.
    reflected = {"uuid-live": AutomationRuleSnapshot(uuid="uuid-live", name="VULN: revert control-field edits")}
    changes, _ = plan_tmp_automation(rules, reflected, state, {})
    assert [(c.alias, c.action) for c in changes if isinstance(c, UpsertAutomationRule)] == [
        ("vuln_control_guard", "adopt")
    ]

    # Tracked and identical (refs from a previous apply recorded in state):
    # no change at all — the idempotence the apply path depends on. The
    # reflected payload is what a previous apply produced: the declared
    # payload with the recorded refs substituted.
    record = rules["vuln_control_guard"]
    recorded_refs = {name: f"resolved-{name}" for name, _kind, _declared in record.refs}
    from stint.schema.automation import ref_placeholder, substitute_refs

    reflected_payload = substitute_refs(
        json.loads(json.dumps(record.payload)),
        {
            ref_placeholder(kind, name): value
            for (name, kind, _d), value in zip(record.refs, recorded_refs.values(), strict=True)
        },
    )
    reflected = {"uuid-live": AutomationRuleSnapshot(uuid="uuid-live", name=record.name, payload=reflected_payload)}
    state.automation_rules["vuln_control_guard"] = AutomationRuleMapping(
        id="uuid-live",
        name=record.name,
        enabled=record.enabled,
        scope_projects=list(record.scope_projects),
        refs=recorded_refs,
    )
    changes, warnings = plan_tmp_automation(rules, reflected, state, {})
    assert not [c for c in changes if isinstance(c, UpsertAutomationRule)]
    assert not warnings

    # Payload drift (a real structural change: the comment action dropped —
    # a description-only change is invisible to the diff by design, the op
    # parameters own it): the recorded refs make the comparison decisive.
    changed_rule = automation_rule_record(
        _tmp_guard_rule(on_violation=["revert"]),
        bound_projects=("VULN",),
        proj_cls=None,
    )
    changed_rules = {"vuln_control_guard": changed_rule}
    changes, _ = plan_tmp_automation(changed_rules, reflected, state, {})
    assert [(c.alias, c.action) for c in changes if isinstance(c, UpsertAutomationRule)] == [
        ("vuln_control_guard", "update")
    ]

    # allow_delete removes a state-tracked rule no longer declared.
    changes, _ = plan_tmp_automation({}, reflected, state, {}, allow_delete=True)
    assert [c for c in changes if isinstance(c, DeleteAutomationRule)][0].alias == "vuln_control_guard"


@pytest.mark.asyncio
@respx.mock
async def test_apply_tmp_automation_creates_guard_rule():
    """The end-to-end TMP create: plan -> apply -> the complete payload on
    the wire (guard check, resolved refs, scope ARI, actor) -> state."""
    import json

    mock = respx.mock
    mock.get(f"{BASE}/_edge/tenant_info").mock(return_value=httpx.Response(200, json={"cloudId": CLOUD_ID_TMP}))
    mock.get(f"{CLOUD_ROOT}/myself").mock(return_value=httpx.Response(200, json={"accountId": "acct-me"}))
    mock.get(f"{CLOUD_ROOT}/users/search").mock(
        return_value=httpx.Response(
            200, json=[{"accountType": "app", "displayName": "Automation for Jira", "accountId": "557058:app"}]
        )
    )
    mock.get(f"{CLOUD_ROOT}/user/search").mock(
        return_value=httpx.Response(200, json=[{"accountId": "acct-me", "emailAddress": "me@example.com"}])
    )
    mock.get(f"{CLOUD_ROOT}/user", params={"accountId": "acct-me", "expand": "groups"}).mock(
        return_value=httpx.Response(200, json={"groups": {"items": []}})
    )
    mock.get(f"{AUTOMATION_ROOT}/rule/summary").mock(return_value=httpx.Response(200, json={"links": {}, "data": []}))
    posted = mock.post(f"{AUTOMATION_ROOT}/rule").mock(return_value=httpx.Response(201, json={"ruleUuid": "uuid-new"}))

    project_cls = _tmp_schema_project()
    from stint.dialects.jira.tmp.desired import build_tmp_desired

    desired = build_tmp_desired(project_cls)

    state = StateFile(env="devel", jira_url=BASE)
    state.projects["VULN"] = ProjectMapping(id="10001", style="team-managed", key="VULN")
    tmp_state = TmpState(fields={"vuln_severity": "customfield_10001"})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # the dialect's experimental notice
        dialect = TmpDialect(_tmp_client_of(mock))
        ctx = TmpApplyContext(
            dialect=dialect,
            project=TmpProjectContext(
                cloud_id=CLOUD_ID_TMP, project_id="10001", project_uuid="p-uuid", key="VULN", name="Vuln"
            ),
            state=tmp_state,
            file_state=state,
        )
        await tmp_upsert_automation_rule(
            ctx,
            UpsertAutomationRule(alias="vuln_control_guard", action="create"),
            desired.automation_rules,
            {},
        )
    body = json.loads(posted.calls[0].request.read())
    rule = body["rule"]
    assert rule["uuid"]
    assert rule["actor"] == {"type": "ACCOUNT_ID", "actor": "557058:app"}
    assert rule["authorAccountId"] == "acct-me"
    assert rule["ruleScopeARIs"] == [f"ari:cloud:jira:{CLOUD_ID_TMP}:project/10001"]
    # The guarded field id resolved through the TMP field map, and the
    # restore value is the verified SMART operations form.
    edit = next(c for c in rule["components"] if c["type"] == "jira.issue.edit")
    assert edit["value"]["operations"][0]["field"] == {"type": "ID", "value": "customfield_10001"}
    assert edit["value"]["operations"][0]["value"] == {"type": "SMART", "value": "{{fieldChange.from}}"}
    assert state.automation_rules["vuln_control_guard"].id == "uuid-new"


def _tmp_client_of(mock: respx.MockRouter) -> Any:
    """A real client whose calls route through the respx mock."""
    from stint.client.http import JiraHTTPClient

    return JiraHTTPClient(BASE, PATAuth("tok"))
