"""Automation rule schemes and the ControlFieldGuard.

Covers the schema declarations (AutomationRule from a payload file with
``{{stint.*}}`` refs, the generated ControlFieldGuard, AutomationRuleScheme,
the Project binding), ref substitution, the diff family (greenfield create /
adoption by name / normalized-payload idempotence / state and scope flips /
orphan deletes behind --allow-delete), emit rendering, the ops against
respx-mocked rule-management endpoints (probe, create, adopt, update, state,
scope, delete), guard allow-list enforcement, and the check/audit
integration.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
import respx

from stint import (
    ConfigurationError,
    CustomField,
    Group,
    IssueType,
    LabelsField,
    NumberField,
    PATAuth,
    Project,
    ProjectRole,
    RegistryError,
    Screen,
    ScreenScheme,
    SelectField,
    StateFile,
    User,
    UserField,
    create_engine,
    op,
    registry,
    validate,
)
from stint.autogen.desired import build_desired_snapshot
from stint.autogen.diff import (
    AdoptAutomationRule,
    CreateAutomationRule,
    DeleteAutomationRule,
    SetAutomationRuleScope,
    SetAutomationRuleState,
    UpdateAutomationRule,
    diff,
)
from stint.autogen.emit import sort_changes
from stint.dialects.jira.cloud import JiraCloudDialect
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.schema.automation import (
    AutomationRule,
    AutomationRuleScheme,
    ControlFieldGuard,
    normalized_rule_payload,
    ref_placeholder,
    substitute_refs,
    unresolved_refs,
)
from stint.state.file import AutomationRuleMapping, CustomFieldMapping, ProjectMapping
from stint.state.snapshot import (
    AutomationRuleSnapshot,
    PermissionGrantSnapshot,
    PermissionSchemeSnapshot,
    ProjectSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)
from tests._stubs import paginated, stub_project_roles_and_permission_schemes

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"
CLOUD_ID = "182b9218-d56a-453d-9659-3f29ea2aa7eb"
AUTOMATION_ROOT = f"https://api.atlassian.com/automation/public/jira/{CLOUD_ID}/rest/v1"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    yield
    registry.reset()


# ── Schema declarations ──────────────────────────────────────────────
def _notify_payload_file(tmp_path: Any) -> Any:
    payload = {
        "name": "will be overridden",
        "trigger": {"component": "TRIGGER", "type": "jira.issue.event.trigger:updated", "value": {}},
        "components": [
            {
                "component": "ACTION",
                "type": "jira.issue.comment",
                "value": {"comment": "severity: {{stint.field.severity}} owner: {{stint.user.owner}}"},
            }
        ],
    }
    path = tmp_path / "notify_on_close.json"
    path.write_text(json.dumps(payload))
    return path


def _rule(tmp_path: Any, **overrides: Any) -> AutomationRule:
    severity = CustomField(alias="severity", name="Severity", type=SelectField, options=["Low", "High"])
    kwargs: dict[str, Any] = {
        "alias": "trk_notify_on_close",
        "name": "TRK: notify owner on close",
        "payload": _notify_payload_file(tmp_path),
        "refs": {"severity": severity, "owner": User("owner@example.com")},
    }
    kwargs.update(overrides)
    return AutomationRule(**kwargs)


def test_automation_rule_registers_and_loads_payload(tmp_path: Any):
    rule = _rule(tmp_path)
    assert registry.automation_rules["trk_notify_on_close"] is rule
    assert rule.name == "TRK: notify owner on close"
    assert rule.payload_data["components"][0]["value"]["comment"].startswith("severity: {{stint.field.severity}}")
    # refs validate and keep declaration order
    assert rule.ref_specs() == [
        ("severity", "field", "severity"),
        ("owner", "user", "owner@example.com"),
    ]


def test_automation_rule_rejects_bad_payload_and_refs(tmp_path: Any):
    with pytest.raises(RegistryError, match="non-empty alias"):
        _rule(tmp_path, alias="")
    with pytest.raises(ConfigurationError, match="cannot read payload"):
        AutomationRule(alias="x", payload=tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        AutomationRule(alias="x", payload=bad)
    with pytest.raises(RegistryError, match="unsupported refs value"):
        AutomationRule(alias="x", payload=_notify_payload_file(tmp_path), refs={"f": 123})


def test_control_field_guard_registers_and_validates():
    guard = _guard()
    assert registry.automation_rules["trk_control_guard"] is guard
    assert guard.is_guard and guard.name == "TRK: revert control-field edits"
    with pytest.raises(RegistryError, match="allowed_writers is required"):
        ControlFieldGuard(alias="bad", allowed_writers=[])
    with pytest.raises(RegistryError, match="unsupported on_violation"):
        ControlFieldGuard(alias="bad", allowed_writers=[User("svc@example.com")], on_violation=["revert", "webhook"])
    with pytest.raises(RegistryError, match="must include 'revert'"):
        ControlFieldGuard(alias="bad", allowed_writers=[User("svc@example.com")], on_violation=["comment"])


def _guard(**overrides: Any) -> ControlFieldGuard:
    kwargs: dict[str, Any] = {
        "alias": "trk_control_guard",
        "name": "TRK: revert control-field edits",
        "allowed_writers": [User("svc-automation@example.com"), ProjectRole("Administrators")],
    }
    kwargs.update(overrides)
    return ControlFieldGuard(**kwargs)


def test_automation_rule_scheme_registers():
    guard = _guard()
    scheme = AutomationRuleScheme(alias="trk_rules", name="TRK Automation", rules=[guard])
    assert registry.automation_rule_schemes["trk_rules"] is scheme
    with pytest.raises(RegistryError, match="duplicate rule names"):
        AutomationRuleScheme(alias="bad", rules=[_guard(), _guard(alias="other_guard")])


def test_project_rejects_team_managed_automation_scheme():
    class Bug(IssueType):
        __alias__ = "bug"

    scheme = AutomationRuleScheme(alias="trk_rules", rules=[_guard()])
    with pytest.raises(ConfigurationError, match="team-managed but references an AutomationRuleScheme"):

        class Bad(Project):
            __key__ = "BAD"
            __issuetypes__ = [Bug]
            __style__ = "team-managed"
            __automation_rule_scheme__ = scheme


def test_validate_flags_undeclared_guard_role():
    scheme = AutomationRuleScheme(alias="trk_rules", rules=[_guard()])

    class Bug(IssueType):
        __alias__ = "bug"

    class Tracked(Project):
        __key__ = "TRK"
        __issuetypes__ = [Bug]
        __automation_rule_scheme__ = scheme

    problems = validate()
    assert any("trk_control_guard" in p and "Administrators" in p for p in problems)


# ── Ref substitution ─────────────────────────────────────────────────
def test_ref_placeholder_kinds():
    assert ref_placeholder("field", "severity") == "{{stint.field.severity}}"
    assert ref_placeholder("user", "owner") == "{{stint.user.owner}}"
    with pytest.raises(RegistryError, match="unknown stint ref kind"):
        ref_placeholder("galaxy", "x")


def test_substitute_refs_handles_keys_and_nested_smart_values():
    payload = {
        "{{stint.field.severity}}": "{{#changelog.{{stint.field.severity}}}}{{from}}{{/}}",
        "components": [{"value": "owner {{stint.user.owner}}"}],
    }
    resolved = {
        "{{stint.field.severity}}": "customfield_10056",
        "{{stint.user.owner}}": "acc-123",
    }
    out = substitute_refs(payload, resolved)
    assert out["customfield_10056"] == "{{#changelog.customfield_10056}}{{from}}{{/}}"
    assert out["components"][0]["value"] == "owner acc-123"


def test_unresolved_refs_reports_leftovers():
    assert unresolved_refs({"a": "{{stint.field.ghost}}"}) == ["{{stint.field.ghost}}"]
    assert unresolved_refs({"a": "clean"}) == []


# ── Guard payload generation ─────────────────────────────────────────
def test_build_guard_payload_trigger_condition_actions():
    severity = CustomField(alias="severity", name="Severity", type=SelectField, options=["Low", "High"])
    owner = CustomField(alias="owner", name="Owner", type=UserField)
    cve = CustomField(alias="cve_ids", name="CVE Ids", type=LabelsField, options=[])
    guard = ControlFieldGuard(
        alias="guard",
        name="TRK: revert control-field edits",
        fields=[severity, owner, cve, "Priority"],
        allowed_writers=[User("svc@example.com"), Group("on-call"), ProjectRole("Administrators")],
        on_violation=["revert", "comment"],
    )
    payload = guard.build_payload([severity, owner, cve, "Priority"])
    assert payload["name"] == "TRK: revert control-field edits"
    assert payload["canOtherRuleTrigger"] is False
    trigger = payload["trigger"]
    assert trigger["type"] == "jira.issue.field.changed"
    assert trigger["value"]["actions"] == ["edit"]  # create stays allowed
    assert trigger["value"]["fields"] == [
        {"value": "{{stint.field.severity}}", "type": "field"},
        {"value": "{{stint.field.owner}}", "type": "field"},
        {"value": "{{stint.field.cve_ids}}", "type": "field"},
        {"value": "priority", "type": "field"},  # system field inlined
    ]
    condition_components = [c for c in payload["components"] if c["type"] == "jira.user.condition"]
    # One condition component per writer kind (mixing checks crashes the
    # create on this build); chained components AND implicitly = the AND of
    # negations (De Morgan for "not any allowed writer").
    assert len(condition_components) == 3
    checks: dict[str, Any] = {}
    for c in condition_components:
        (entry,) = c["value"]["conditions"]
        checks[entry["check"]] = entry["criteria"]
        assert c["value"]["operator"] == "OR"
    assert checks["USER_IS_NOT"] == [{"type": "ID", "value": "{{stint.user.writer0}}"}]
    assert checks["NOT_IN_GROUP"] == [{"type": "ID", "value": "{{stint.group.writer1}}"}]
    assert checks["NOT_IN_ROLE"] == [{"type": "ID", "value": "{{stint.role.writer2}}"}]
    (edit,) = [c for c in payload["components"] if c["type"] == "jira.issue.edit"]
    advanced = json.loads(edit["value"]["advancedFields"])
    # Selects restore by option id ({"id": ...}); user fields by accountId;
    # labels split the previous space-joined set. `fieldChange.from` is the
    # raw previous value on this trigger (changelog is empty there — measured).
    assert advanced["fields"]["{{stint.field.severity}}"] == {"id": "{{fieldChange.from}}"}
    assert advanced["fields"]["{{stint.field.owner}}"] == {"accountId": "{{fieldChange.from}}"}
    assert '.split(" ")' in advanced["fields"]["{{stint.field.cve_ids}}"]  # labels restore as a set
    assert advanced["fields"]["priority"] == "{{fieldChange.from}}"
    assert edit["value"]["sendNotifications"] is False  # the bad value must not page watchers twice
    (comment,) = [c for c in payload["components"] if c["type"] == "jira.issue.comment"]
    assert "[~accountid:{{initiator.accountId}}]" in comment["value"]["comment"]


def test_guard_without_comment_omits_comment_action():
    guard = _guard(on_violation=["revert"])
    payload = guard.build_payload(["Priority"])
    assert not [c for c in payload["components"] if c["type"] == "jira.issue.comment"]


def test_guard_restore_forms_by_field_type():
    number = CustomField(alias="score", name="Score", type=NumberField, options=[])
    assert guard_restore(number) == "{{fieldChange.from}}"


def guard_restore(field: CustomField) -> str:
    from stint.schema.automation import _custom_field_restore

    return _custom_field_restore(field, ref_placeholder("field", field.alias))


# ── Desired snapshot ─────────────────────────────────────────────────
def _controlled_schema(**guard_overrides: Any) -> tuple[type[Project], ControlFieldGuard]:
    screen_scheme = _screen_scheme()

    class Bug(IssueType):
        __alias__ = "bug"
        __screen_scheme__ = screen_scheme

    guard = _guard(**guard_overrides)
    scheme = AutomationRuleScheme(alias="trk_rules", name="TRK Automation", rules=[guard])

    class Tracked(Project):
        __key__ = "TRK"
        __issuetypes__ = [Bug]
        __automation_rule_scheme__ = scheme

    return Tracked, guard


def _screen_scheme() -> ScreenScheme:
    severity = CustomField(alias="severity", name="Severity", type=SelectField, options=["Low", "High"])
    create_screen = Screen(alias="trk_create", name="TRK Create", fields=["Summary", severity])
    edit_screen = Screen(alias="trk_edit", name="TRK Edit", fields=["Summary"])  # severity locked
    view_screen = Screen(alias="trk_view", name="TRK View", fields=["Summary", severity])
    return ScreenScheme(
        alias="trk_screens", name="TRK Screens", create=create_screen, edit=edit_screen, view=view_screen
    )


def test_desired_guard_derives_control_fields():
    project_cls, guard = _controlled_schema()
    desired = build_desired_snapshot()
    rule = desired.automation_rules["trk_control_guard"]
    assert rule.is_guard
    assert rule.payload["trigger"]["value"]["fields"] == [{"value": "{{stint.field.severity}}", "type": "field"}]
    assert rule.allowed_writers == (("user", "svc-automation@example.com"), ("role", "Administrators"))
    assert rule.scope_projects == ("TRK",)
    assert desired.projects["TRK"].automation_rule_scheme == "trk_rules"


def test_desired_guard_explicit_fields_win():
    _controlled_schema(fields=[CustomField(alias="explicit", name="Explicit", type=NumberField, options=[])])
    desired = build_desired_snapshot()
    rule = desired.automation_rules["trk_control_guard"]
    assert rule.payload["trigger"]["value"]["fields"] == [{"value": "{{stint.field.explicit}}", "type": "field"}]


def test_desired_guard_unbound_without_fields_raises():
    with pytest.raises(RegistryError, match="no bound project"):
        ControlFieldGuard(alias="loose", name="Loose", allowed_writers=[User("svc@example.com")]).field_refs()


def test_desired_guard_no_control_fields_raises():
    severity = CustomField(alias="severity", name="Severity", type=SelectField, options=["Low", "High"])
    screen = Screen(alias="one_screen", name="One", fields=["Summary", severity])
    screen_scheme = ScreenScheme(alias="same", name="Same", create=screen, edit=screen, view=screen)

    class Bug(IssueType):
        __alias__ = "bug"
        __screen_scheme__ = screen_scheme

    guard = _guard()
    scheme = AutomationRuleScheme(alias="rules", rules=[guard])

    class Flat(Project):
        __key__ = "FLT"
        __issuetypes__ = [Bug]
        __automation_rule_scheme__ = scheme

    with pytest.raises(RegistryError, match="derives no control fields"):
        build_desired_snapshot()


def test_desired_plain_rule_carries_payload_and_refs(tmp_path: Any):
    rule = _rule(tmp_path)
    AutomationRuleScheme(alias="rules", rules=[rule])

    class Bug(IssueType):
        __alias__ = "bug"

    class Tracked(Project):
        __key__ = "TRK"
        __issuetypes__ = [Bug]
        __automation_rule_scheme__ = AutomationRuleScheme(alias="rules2", rules=[rule])

    desired = build_desired_snapshot()
    want = desired.automation_rules["trk_notify_on_close"]
    assert want.payload == rule.payload_data
    assert want.refs == (("severity", "field", "severity"), ("owner", "user", "owner@example.com"))
    assert not want.is_guard


# ── Diff ─────────────────────────────────────────────────────────────
def _snapshot() -> Snapshot:
    snap = Snapshot(server_info=ServerInfoSnapshot(deployment_type="Cloud", version="1", base_url=BASE))
    snap.projects["TRK"] = ProjectSnapshot(
        id="10010", key="TRK", name="TRK", style="classic", permission_scheme_id="10000"
    )
    snap.permission_schemes["10000"] = PermissionSchemeSnapshot(id="10000", name="Default")
    return snap


def _state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE, cloud_id=CLOUD_ID)
    state.projects["TRK"] = ProjectMapping(id="10010", key="TRK")
    return state


def _rule_snapshot(
    name: str, payload: dict[str, Any], *, state: str = "ENABLED", scope: tuple[str, ...] = ()
) -> AutomationRuleSnapshot:
    return AutomationRuleSnapshot(
        uuid="0192e5ac-0e25-71de-ba7b-d972e6a2049a", name=name, state=state, scope_apis=scope, payload=payload
    )


def test_diff_greenfield_emits_create_and_scope():
    _controlled_schema()
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=_state())
    changes = sort_changes(result.changes)
    kinds = [type(c).__name__ for c in changes]
    assert "CreateAutomationRule" in kinds and "SetAutomationRuleScope" in kinds
    assert kinds.index("CreateAutomationRule") < kinds.index("SetAutomationRuleScope")
    (scope,) = [c for c in changes if isinstance(c, SetAutomationRuleScope)]
    assert scope.scope_projects == ("TRK",)
    assert not result.warnings


def test_diff_adopts_existing_rule_by_name():
    _controlled_schema()
    snap = _snapshot()
    guard_payload = build_desired_snapshot().automation_rules["trk_control_guard"].payload
    snap.automation_rules["uuid-1"] = _rule_snapshot("TRK: revert control-field edits", guard_payload)
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=_state())
    assert any(isinstance(c, AdoptAutomationRule) for c in result.changes)
    assert not any(isinstance(c, CreateAutomationRule) for c in result.changes)


def test_diff_idempotent_when_payload_matches():
    _controlled_schema()
    state = _state()
    want = build_desired_snapshot().automation_rules["trk_control_guard"]
    # The live rule: same shape, server ids/noise, real tenant ids in place
    # of the placeholders (as a GET rule/{uuid} would answer). The resolved
    # ids come from the state mapping's refs record (what the last apply
    # substituted).
    mapping_refs = {"severity": "customfield_10056", "writer0": "acct-svc", "writer1": "10360"}
    resolved = {ref_placeholder(kind, name): mapping_refs[name] for name, kind, _value in want.refs}
    reflected = substitute_refs(json.loads(json.dumps(want.payload)), resolved)
    reflected["name"] = want.name
    reflected["id"] = "151"
    reflected["created"] = 1743568964.174
    reflected["updated"] = 1743568964.174
    reflected["tags"] = [{"id": 1, "tagType": "IS_RULE_UPDATED", "tagValue": "true"}]
    reflected["components"] = [dict(c, id=str(100 + i)) for i, c in enumerate(reflected["components"])]
    reflected["actor"] = {"type": "ACCOUNT_ID", "value": "557058:automation"}
    state.automation_rules["trk_control_guard"] = AutomationRuleMapping(
        id="uuid-1", name=want.name, refs={"severity": "customfield_10056", "writer0": "acct-svc", "writer1": "10360"}
    )
    snap = _snapshot()
    snap.automation_rules["uuid-1"] = _rule_snapshot(
        want.name, reflected, scope=(f"ari:cloud:jira:{CLOUD_ID}:project/10010",)
    )
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=state)
    # No schema change: no payload update, no state flip, no scope churn.
    assert not [
        c
        for c in result.changes
        if isinstance(c, (UpdateAutomationRule, SetAutomationRuleState, SetAutomationRuleScope))
    ]
    assert not result.warnings


def test_diff_flags_payload_drift():
    _controlled_schema()
    state = _state()
    want = build_desired_snapshot().automation_rules["trk_control_guard"]
    mapping_refs = {"severity": "customfield_10056", "writer0": "acct-svc", "writer1": "10360"}
    resolved = {ref_placeholder(kind, name): mapping_refs[name] for name, kind, _value in want.refs}
    reflected = substitute_refs(json.loads(json.dumps(want.payload)), resolved)
    reflected["components"][0]["value"]["conditions"][0]["check"] = "USER_IS"  # inverted: now allows nobody
    state.custom_fields["severity"] = CustomFieldMapping(id="customfield_10056")
    state.automation_rules["trk_control_guard"] = AutomationRuleMapping(id="uuid-1", name=want.name, refs=mapping_refs)
    snap = _snapshot()
    snap.automation_rules["uuid-1"] = _rule_snapshot(want.name, reflected)
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=state)
    assert any(isinstance(c, UpdateAutomationRule) for c in result.changes)


def test_diff_state_flip_and_scope_drift():
    _controlled_schema()
    state = _state()
    want = build_desired_snapshot().automation_rules["trk_control_guard"]
    state.automation_rules["trk_control_guard"] = AutomationRuleMapping(id="uuid-1", name=want.name, enabled=False)
    snap = _snapshot()
    snap.automation_rules["uuid-1"] = _rule_snapshot(want.name, want.payload, state="DISABLED", scope=())
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=state)
    (flip,) = [c for c in result.changes if isinstance(c, SetAutomationRuleState)]
    assert flip.enabled is True
    assert any(isinstance(c, SetAutomationRuleScope) for c in result.changes)


def test_diff_unresolvable_refs_skip_with_warning(tmp_path: Any):
    _rule(tmp_path, refs={"severity": CustomField(alias="ghost", name="Ghost", type=NumberField, options=[])})
    AutomationRuleScheme(alias="rules", rules=[registry.automation_rules["trk_notify_on_close"]])
    state = _state()
    state.automation_rules["trk_notify_on_close"] = AutomationRuleMapping(
        id="uuid-9", name="TRK: notify owner on close"
    )
    snap = _snapshot()
    snap.automation_rules["uuid-9"] = _rule_snapshot("TRK: notify owner on close", {"name": "x", "components": []})
    result = diff(desired=build_desired_snapshot(), snapshot=snap, state=state, member_params={})
    assert not [c for c in result.changes if isinstance(c, UpdateAutomationRule)]
    assert any("payload comparison skipped" in w for w in result.warnings)


def test_diff_orphan_delete_behind_allow_delete():
    _controlled_schema()
    state = _state()
    state.automation_rules["orphan_rule"] = AutomationRuleMapping(id="dead-beef", name="Orphan")
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=state, allow_delete=False)
    assert not [c for c in result.changes if isinstance(c, DeleteAutomationRule)]
    assert any("orphan_rule" in w for w in result.warnings)
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=state, allow_delete=True)
    assert any(isinstance(c, DeleteAutomationRule) for c in result.changes)


def test_normalized_rule_payload_ignores_server_noise():
    declared = {"name": "R", "components": [{"type": "t", "value": "v"}]}
    reflected = {
        "name": "R",
        "id": "151",
        "created": 1,
        "updated": 2,
        "uuid": "u",
        "tags": [],
        "authorAccountId": "a",
        "collaborators": [],
        "state": "ENABLED",
        "ruleScopeARIs": ["ari"],
        "components": [{"id": "151", "type": "t", "value": "v"}],
    }
    assert normalized_rule_payload(declared, is_guard=False) == normalized_rule_payload(reflected, is_guard=False)
    # Guards drop the actor (stint omits it; Jira reports the A4J id).
    with_actor = dict(declared, actor={"type": "ACCOUNT_ID", "value": "557058:x"})
    assert normalized_rule_payload(with_actor, is_guard=True) == normalized_rule_payload(declared, is_guard=True)
    assert normalized_rule_payload(with_actor, is_guard=False) != normalized_rule_payload(declared, is_guard=False)


# ── Emit ─────────────────────────────────────────────────────────────
def test_render_automation_rule_roundtrips_through_compile():
    _controlled_schema()
    result = diff(desired=build_desired_snapshot(), snapshot=_snapshot(), state=_state())
    sources = []
    for change in result.changes:
        from stint.autogen.emit import _render_one

        sources.append(_render_one(change))
    body = "\n".join(sources)
    compile(f"async def upgrade():\n{body}", "<gen>", "exec")  # syntax-valid op calls
    assert "op.create_automation_rule(" in body
    assert "__import__('json').loads(" in body
    assert "is_guard=True" in body
    assert "dict(kind='user', value='svc-automation@example.com')" in body


# ── Ops ──────────────────────────────────────────────────────────────
def _engine() -> Any:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


async def _run_in_ctx(engine: Any, state: StateFile, body: Any) -> Any:
    ctx = MigrationContext(engine=engine, state=state, direction="upgrade")
    token = set_context(ctx)
    try:
        return await body()
    finally:
        reset_context(token)


def _stub_automation(mock: respx.MockRouter, *, summaries: list[dict[str, Any]] | None = None) -> None:
    """The rule-management reads every automation op starts with."""
    mock.get(f"{BASE}/_edge/tenant_info").mock(return_value=httpx.Response(200, json={"cloudId": CLOUD_ID}))
    mock.get(f"{AUTOMATION_ROOT}/rule/summary").mock(
        return_value=httpx.Response(200, json={"links": {}, "data": summaries or []})
    )
    stub_project_roles_and_permission_schemes(mock, CLOUD_ROOT)
    mock.get(f"{CLOUD_ROOT}/myself").mock(return_value=httpx.Response(200, json={"accountId": "acct-svc"}))
    mock.get(f"{CLOUD_ROOT}/user").mock(return_value=httpx.Response(200, json={"groups": {"size": 0, "items": []}}))
    mock.get(f"{CLOUD_ROOT}/role").mock(
        return_value=httpx.Response(200, json=paginated([{"id": "10360", "name": "Administrators"}]))
    )
    mock.get(f"{CLOUD_ROOT}/group/bulk").mock(
        return_value=httpx.Response(200, json={"values": [{"groupId": "g-1", "name": "on-call"}], "isLast": True})
    )
    # Query-aware: each email resolves to its own accountId (no GDPR
    # sole-result fallback masking an allow-list mismatch).
    mock.get(f"{CLOUD_ROOT}/user/search").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=(
                [{"accountId": "acct-svc", "emailAddress": "svc-automation@example.com"}]
                if "svc-automation" in str(request.url)
                else [{"accountId": "acct-stranger", "emailAddress": "someone-else@example.com"}]
            ),
        )
    )
    # The "Automation for Jira" app account (create's default actor), via
    # the offset-paginated users/search.
    mock.get(f"{CLOUD_ROOT}/users/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"accountId": "557058:a4j", "accountType": "app", "displayName": "Automation for Jira", "active": True}
            ],
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_create_automation_rule_resolves_refs_and_records_state(tmp_path: Any):
    mock = respx.mock
    _stub_automation(mock)
    create_route = mock.post(f"{AUTOMATION_ROOT}/rule").mock(
        return_value=httpx.Response(201, json={"ruleUuid": "uuid-new"})
    )
    engine = _engine()
    state = _state()
    state.custom_fields["severity"] = CustomFieldMapping(id="customfield_10056")

    rule = _rule(tmp_path)
    AutomationRuleScheme(alias="rules", rules=[rule])
    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_automation_rule(
                alias="trk_notify_on_close",
                name="TRK: notify owner on close",
                description="",
                payload=rule.payload_data,
                refs=[
                    dict(ref="severity", kind="field", value="severity"),
                    dict(ref="owner", kind="user", value="owner@example.com"),
                ],
                enabled=True,
            ),
        )

    payload = json.loads(create_route.calls.last.request.read())["rule"]
    assert payload["name"] == "TRK: notify owner on close"  # declaration wins
    assert payload["state"] == "ENABLED"
    comment = payload["components"][0]["value"]["comment"]
    assert "customfield_10056" in comment and "acc-svc-1" not in comment  # owner resolves via user search
    mapping = state.automation_rules["trk_notify_on_close"]
    assert mapping.id == "uuid-new"
    assert mapping.enabled is True
    assert mapping.refs["severity"] == "customfield_10056"


@pytest.mark.asyncio
@respx.mock
async def test_create_automation_rule_refuses_name_collision(tmp_path: Any):
    mock = respx.mock
    _stub_automation(mock, summaries=[{"uuid": "existing", "name": "TRK: notify owner on close", "state": "ENABLED"}])
    engine = _engine()
    state = _state()
    rule = _rule(tmp_path)
    async with engine:
        with pytest.raises(ConfigurationError, match="already exists"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_automation_rule(
                    alias="trk_notify_on_close",
                    name="TRK: notify owner on close",
                    description="",
                    payload=rule.payload_data,
                    refs=[],
                ),
            )


@pytest.mark.asyncio
@respx.mock
async def test_create_automation_rule_probe_403_fails_before_write(tmp_path: Any):
    mock = respx.mock
    mock.get(f"{BASE}/_edge/tenant_info").mock(return_value=httpx.Response(200, json={"cloudId": CLOUD_ID}))
    mock.get(f"{AUTOMATION_ROOT}/rule/summary").mock(return_value=httpx.Response(403, json={"errors": []}))
    create_route = mock.post(f"{AUTOMATION_ROOT}/rule").mock(return_value=httpx.Response(201, json={"ruleUuid": "x"}))
    engine = _engine()
    state = _state()
    rule = _rule(tmp_path)
    async with engine:
        with pytest.raises(Exception, match="cannot manage rules|403"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_automation_rule(
                    alias="r", name="R", description="", payload=rule.payload_data, refs=[]
                ),
            )
    assert not [c for c in create_route.calls]


@pytest.mark.asyncio
@respx.mock
async def test_guard_writer_check_refuses_and_allows():
    mock = respx.mock
    _stub_automation(mock)
    engine = _engine()
    state = _state()

    # acct-svc is the authenticated account (myself stub); the allow-list
    # names it directly, so the guard passes.
    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op._guard_allowed_writer_check(
                engine.dialect,
                "guard",
                [("user", "svc-automation@example.com")],
                allow_lockout=False,
            ),
        )
        with pytest.raises(Exception, match="not among the guard's allowed writers"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op._guard_allowed_writer_check(
                    engine.dialect,
                    "guard",
                    [("user", "someone-else@example.com")],
                    allow_lockout=False,
                ),
            )
        # Escape hatch, like the other guards.
        await _run_in_ctx(
            engine,
            state,
            lambda: op._guard_allowed_writer_check(
                engine.dialect,
                "guard",
                [("user", "someone-else@example.com")],
                allow_lockout=True,
            ),
        )


@pytest.mark.asyncio
@respx.mock
async def test_update_automation_rule_puts_resolved_payload(tmp_path: Any):
    mock = respx.mock
    _stub_automation(mock)
    put_route = mock.put(url__regex=r"^" + re.escape(AUTOMATION_ROOT) + r"/rule/[^/]+$").mock(
        return_value=httpx.Response(200, json={"ruleUuid": "uuid-1"})
    )
    engine = _engine()
    state = _state()
    state.custom_fields["severity"] = CustomFieldMapping(id="customfield_10056")
    state.automation_rules["trk_notify_on_close"] = AutomationRuleMapping(id="uuid-1", name="old name")
    rule = _rule(tmp_path)
    async with engine:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_automation_rule(
                alias="trk_notify_on_close",
                name="TRK: notify owner on close",
                description="updated",
                payload=rule.payload_data,
                refs=[
                    dict(ref="severity", kind="field", value="severity"),
                    dict(ref="owner", kind="user", value="owner@example.com"),
                ],
            ),
        )
    body = json.loads(put_route.calls.last.request.read())["rule"]
    assert body["name"] == "TRK: notify owner on close"
    assert "customfield_10056" in body["components"][0]["value"]["comment"]
    assert state.automation_rules["trk_notify_on_close"].name == "TRK: notify owner on close"


@pytest.mark.asyncio
@respx.mock
async def test_set_state_and_scope_and_delete():
    mock = respx.mock
    _stub_automation(mock)
    state_route = mock.put(f"{AUTOMATION_ROOT}/rule/uuid-1/state").mock(return_value=httpx.Response(200))
    scope_route = mock.put(f"{AUTOMATION_ROOT}/rule/uuid-1/rule-scope").mock(return_value=httpx.Response(200))
    delete_route = mock.delete(f"{AUTOMATION_ROOT}/rule/uuid-1").mock(return_value=httpx.Response(200))
    engine = _engine()
    state = _state()
    state.automation_rules["r"] = AutomationRuleMapping(id="uuid-1", name="R")
    async with engine:
        await _run_in_ctx(engine, state, lambda: op.set_automation_rule_state(alias="r", enabled=False))
        await _run_in_ctx(engine, state, lambda: op.set_automation_rule_scope(alias="r", scope_projects=["TRK"]))
        await _run_in_ctx(engine, state, lambda: op.delete_automation_rule(alias="r"))

    assert json.loads(state_route.calls.last.request.read())["value"] == "DISABLED"
    assert json.loads(scope_route.calls.last.request.read())["ruleScopeARIs"] == [
        f"ari:cloud:jira:{CLOUD_ID}:project/10010"
    ]
    assert state.cloud_id == CLOUD_ID  # resolved once, kept
    # Delete disables first, then deletes; state mapping is gone.
    assert [c.request.method for c in delete_route.calls] == ["DELETE"]
    assert "r" not in state.automation_rules


@pytest.mark.asyncio
@respx.mock
async def test_delete_idempotent_on_missing():
    mock = respx.mock
    _stub_automation(mock)
    mock.put(f"{AUTOMATION_ROOT}/rule/uuid-1/state").mock(return_value=httpx.Response(200))
    mock.delete(f"{AUTOMATION_ROOT}/rule/uuid-1").mock(return_value=httpx.Response(404))
    engine = _engine()
    state = _state()
    state.automation_rules["r"] = AutomationRuleMapping(id="uuid-1", name="R")
    async with engine:
        await _run_in_ctx(engine, state, lambda: op.delete_automation_rule(alias="r"))
    assert "r" not in state.automation_rules


@pytest.mark.asyncio
@respx.mock
async def test_adopt_automation_rule_records_uuid():
    mock = respx.mock
    _stub_automation(mock, summaries=[{"uuid": "uuid-live", "name": "TRK: notify owner on close", "state": "ENABLED"}])
    engine = _engine()
    state = _state()
    async with engine:
        await _run_in_ctx(engine, state, lambda: op.adopt_automation_rule(alias="r", name="TRK: notify owner on close"))
    assert state.automation_rules["r"].id == "uuid-live"
    assert not [c for c in mock.calls if c.request.method == "POST"]


# ── Reflection ───────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_reflect_automation_rules_summaries_then_payloads():
    mock = respx.mock
    mock.get(f"{BASE}/_edge/tenant_info").mock(return_value=httpx.Response(200, json={"cloudId": CLOUD_ID}))
    mock.get(f"{AUTOMATION_ROOT}/rule/summary").mock(
        return_value=httpx.Response(
            200,
            json={
                "links": {},
                "data": [
                    {
                        "uuid": "uuid-1",
                        "name": "TRK: revert control-field edits",
                        "state": "ENABLED",
                        "ruleScopeARIs": [f"ari:cloud:jira:{CLOUD_ID}:project/10010"],
                    }
                ],
            },
        )
    )
    mock.get(f"{AUTOMATION_ROOT}/rule/uuid-1").mock(
        return_value=httpx.Response(200, json={"rule": {"name": "TRK: revert control-field edits", "components": []}})
    )
    dialect = JiraCloudDialect(_client_of(mock))
    rules = await dialect._reflect_automation_rules()
    rule = rules["uuid-1"]
    assert rule.name == "TRK: revert control-field edits"
    assert rule.state == "ENABLED"
    assert rule.scope_apis == (f"ari:cloud:jira:{CLOUD_ID}:project/10010",)
    assert rule.payload["name"] == "TRK: revert control-field edits"


@pytest.mark.asyncio
@respx.mock
async def test_reflect_automation_rules_degrades_on_403():
    mock = respx.mock
    mock.get(f"{BASE}/_edge/tenant_info").mock(return_value=httpx.Response(200, json={"cloudId": CLOUD_ID}))
    mock.get(f"{AUTOMATION_ROOT}/rule/summary").mock(return_value=httpx.Response(403, json={"errors": []}))
    dialect = JiraCloudDialect(_client_of(mock))
    with pytest.warns(UserWarning, match="automation rule reflection skipped"):
        rules = await dialect._reflect_automation_rules()
    assert rules == {}


def _client_of(mock: respx.MockRouter) -> Any:
    """A real client whose calls route through the respx mock."""
    from stint.client.auth import PATAuth
    from stint.client.http import JiraHTTPClient

    return JiraHTTPClient(BASE, PATAuth("tok"))


# ── Check integration ────────────────────────────────────────────────
def _check_snapshot_with_wide_grant() -> Snapshot:
    snap = _snapshot()
    snap.permission_schemes["10000"] = PermissionSchemeSnapshot(
        id="10000",
        name="Default",
        grants=(
            PermissionGrantSnapshot(
                id="g1", permission="EDIT_ISSUES", holder_type="ApplicationRole", holder_parameter=""
            ),
        ),
    )
    return snap


def _check_state_with_guard(*, enabled: bool = True) -> StateFile:
    state = _state()
    want = build_desired_snapshot().automation_rules["trk_control_guard"]
    state.automation_rules["trk_control_guard"] = AutomationRuleMapping(id="uuid-1", name=want.name, enabled=enabled)
    return state


async def _run_check(project_cls: type[Project], state: StateFile, snapshot: Snapshot) -> Any:
    from stint.check import CheckReport, _check_screen_lock_coverage

    report = CheckReport(project=project_cls.__key__)
    engine = _engine()
    async with engine:
        await _check_screen_lock_coverage(engine.dialect, project_cls, state, snapshot, report)
    return report


@pytest.mark.asyncio
@respx.mock
async def test_check_uncovered_control_fields_warn():
    mock = respx.mock
    _stub_automation(mock)
    project_cls, _guard = _controlled_schema()
    report = await _run_check(project_cls, _state(), _check_snapshot_with_wide_grant())
    assert not report.infos
    assert any("cosmetic for API clients" in w for w in report.warnings)
    assert any("severity" in w for w in report.warnings)


@pytest.mark.asyncio
@respx.mock
async def test_check_covered_control_fields_report_info():
    mock = respx.mock
    _stub_automation(mock)
    project_cls, _guard = _controlled_schema()
    state = _check_state_with_guard()
    snap = _check_snapshot_with_wide_grant()
    want = build_desired_snapshot().automation_rules["trk_control_guard"]
    snap.automation_rules["uuid-1"] = _rule_snapshot(want.name, want.payload)
    report = await _run_check(project_cls, state, snap)
    assert not [w for w in report.warnings if "cosmetic" in w]
    assert any("covered by automation guard" in i and "TRK: revert control-field edits" in i for i in report.infos)


@pytest.mark.asyncio
@respx.mock
async def test_check_guard_drift_disabled_and_allow_list():
    mock = respx.mock
    _stub_automation(mock)
    project_cls, _unused_guard = _controlled_schema()
    want = build_desired_snapshot().automation_rules["trk_control_guard"]

    # Disabled in Jira.
    state = _check_state_with_guard(enabled=False)
    snap = _check_snapshot_with_wide_grant()
    snap.automation_rules["uuid-1"] = _rule_snapshot(want.name, want.payload, state="DISABLED")
    report = await _run_check(project_cls, state, snap)
    assert any("guard 'trk_control_guard' is disabled in Jira" in w for w in report.warnings)

    # Allow-list no longer covers the authenticated account.
    drift_guard = _guard(allowed_writers=[User("someone-else@example.com")])
    scheme = AutomationRuleScheme(alias="trk_rules_other", name="TRK Automation", rules=[drift_guard])

    class Bug2(IssueType):
        __alias__ = "bug2"
        __screen_scheme__ = _screen_scheme()

    class Other(Project):
        __key__ = "TRK"
        __issuetypes__ = [Bug2]
        __automation_rule_scheme__ = scheme

    state = _state()
    state.automation_rules["trk_control_guard"] = AutomationRuleMapping(id="uuid-1", name=want.name)
    snap = _check_snapshot_with_wide_grant()
    snap.automation_rules["uuid-1"] = _rule_snapshot(want.name, want.payload)
    report = await _run_check(Other, state, snap)
    assert any("no longer includes the authenticated account" in w for w in report.warnings)


# ── Audit integration ────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_audit_guard_allow_list_and_revert_classification():
    mock = respx.mock
    _stub_automation(mock)
    project_cls, guard = _controlled_schema()
    severity_id = "customfield_10056"

    # One issue: violator writes severity at T1; the guard restores it at T2.
    mock.post(f"{CLOUD_ROOT}/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "TRK-1"}], "isLast": True})
    )
    changelog = {
        "values": [
            {
                "created": "2026-11-25T10:00:00.000+0000",
                "author": {"accountId": "acct-violator"},
                "items": [
                    {
                        "field": "Severity",
                        "fieldId": severity_id,
                        "from": "10001",
                        "fromString": "Low",
                        "to": "10002",
                        "toString": "High",
                    }
                ],
            },
            {
                "created": "2026-11-25T10:00:05.000+0000",
                "author": {"accountId": "acct-guard"},
                "items": [
                    {
                        "field": "Severity",
                        "fieldId": severity_id,
                        "from": "10002",
                        "fromString": "High",
                        "to": "10001",
                        "toString": "Low",
                    }
                ],
            },
        ]
    }
    mock.get(f"{CLOUD_ROOT}/issue/TRK-1/changelog").mock(
        side_effect=lambda request: httpx.Response(200, json={"values": changelog["values"]})
    )

    from stint.audit import audit_control_fields

    engine = _engine()
    state = _state()
    state.custom_fields["severity"] = CustomFieldMapping(id=severity_id)
    async with engine:
        report = await audit_control_fields(engine, project_cls, state, since="2026/11/20 00:00", guard=guard)

    assert [w.author for w in report.writes] == ["acct-violator"]
    assert report.writes[0].reverted is True  # the guard's restore followed
    # raise_for_writes alerts on unreverted only: a clean pass here.
    report.raise_for_writes()

    # Without the restore entry the write is unreverted and raises.
    changelog["values"] = changelog["values"][:1]
    engine2 = _engine()
    async with engine2:
        report = await audit_control_fields(engine2, project_cls, state, since="2026/11/20 00:00", guard=guard)
    assert report.writes[0].reverted is False
    with pytest.raises(Exception, match="unreverted"):
        report.raise_for_writes()
