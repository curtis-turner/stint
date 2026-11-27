"""Diff a DesiredSnapshot against a reflected Snapshot, using the StateFile
as the management boundary.

Rules:
  - Aliases declared in schema but absent from state          → CREATE
  - Aliases in state AND in schema                            → strict per-attribute UPDATE(s)
  - Aliases in state but NOT in schema                        → DELETE (requires allow_delete=True)
  - Objects in Jira not tracked by state and not in schema    → IGNORED (out of management)
  - Aliases in state but their Jira id is absent from snapshot → WARNING (drift)

Emits ordered `Change` records grouped by op family. The sort module turns
this into a final phase-ordered list for emission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stint.autogen.desired import (
    DesiredAutomationRule,
    DesiredSnapshot,
)
from stint.fields import jira_system_field_id
from stint.lockout import restrict_rule_parameters
from stint.schema.automation import (
    normalized_rule_payload,
    ref_placeholder,
    substitute_refs,
    unresolved_refs,
)

if TYPE_CHECKING:
    from stint.state.file import StateFile
    from stint.state.snapshot import Snapshot


# ── Change records ──────────────────────────────────────────────────
@dataclass
class Change:
    """One operation that the emitted migration must run. Subclasses below
    are flat dataclasses; the emit module renders each to an op-call string.
    """

    pass


# Custom fields
@dataclass
class CreateCustomField(Change):
    alias: str
    name: str
    type_id: str
    description: str
    options: tuple[str, ...]
    searcher_key: str = ""


@dataclass
class UpdateCustomField(Change):
    alias: str
    name: str | None = None
    description: str | None = None
    searcher_key: str | None = None


@dataclass
class AddCustomFieldOption(Change):
    field_alias: str
    value: str


@dataclass
class RemoveCustomFieldOption(Change):
    field_alias: str
    value: str


@dataclass
class DeleteCustomField(Change):
    alias: str


# Issue types
@dataclass
class CreateIssueType(Change):
    alias: str
    name: str
    description: str
    subtask: bool


@dataclass
class UpdateIssueType(Change):
    alias: str
    name: str | None = None
    description: str | None = None


@dataclass
class DeleteIssueType(Change):
    alias: str


# Screens
@dataclass
class CreateScreen(Change):
    alias: str
    name: str
    description: str


@dataclass
class AddScreenTab(Change):
    screen_alias: str
    tab_name: str


@dataclass
class AddScreenTabField(Change):
    screen_alias: str
    tab_name: str
    field_alias: str  # custom-field alias OR system field name (string)


@dataclass
class UpdateScreen(Change):
    alias: str
    name: str | None = None
    description: str | None = None


@dataclass
class DeleteScreen(Change):
    alias: str


# Screen schemes
@dataclass
class CreateScreenScheme(Change):
    alias: str
    name: str
    description: str
    screens: dict[str, str]  # op -> screen alias


@dataclass
class UpdateScreenScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None
    screens: dict[str, str] | None = None


@dataclass
class DeleteScreenScheme(Change):
    alias: str


# Field configurations
@dataclass
class CreateFieldConfiguration(Change):
    alias: str
    name: str
    description: str


@dataclass
class UpdateFieldConfiguration(Change):
    alias: str
    name: str | None = None
    description: str | None = None


@dataclass
class SetFieldConfigurationItem(Change):
    fc_alias: str
    field_alias: str
    required: bool
    hidden: bool
    description: str = ""


@dataclass
class DeleteFieldConfiguration(Change):
    alias: str


# Issue-type screen schemes
@dataclass
class CreateIssueTypeScheme(Change):
    alias: str
    name: str
    description: str
    issuetypes: tuple[str, ...]  # ordered issuetype aliases
    default_issuetype: str  # alias; must be in ``issuetypes``


@dataclass
class UpdateIssueTypeScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None
    default_issuetype: str | None = None
    issuetypes: tuple[str, ...] | None = None


@dataclass
class DeleteIssueTypeScheme(Change):
    alias: str


# Issue type screen schemes
@dataclass
class CreateIssueTypeScreenScheme(Change):
    alias: str
    name: str
    description: str
    mappings: dict[str, str]  # issuetype alias (or "default") -> screen-scheme alias


@dataclass
class UpdateIssueTypeScreenScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None
    mappings: dict[str, str] | None = None


@dataclass
class DeleteIssueTypeScreenScheme(Change):
    alias: str


# Field configuration schemes
@dataclass
class CreateFieldConfigurationScheme(Change):
    alias: str
    name: str
    description: str
    mappings: dict[str, str]


@dataclass
class UpdateFieldConfigurationScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None
    mappings: dict[str, str] | None = None


@dataclass
class DeleteFieldConfigurationScheme(Change):
    alias: str


@dataclass
class DeleteIssueSecurityScheme(Change):
    alias: str


# Projects
@dataclass
class CreateProject(Change):
    alias: str
    key: str
    name: str
    project_type_key: str
    lead: str
    description: str
    lead_role: str | None = None


@dataclass
class UpdateProject(Change):
    alias: str
    name: str | None = None
    lead: str | None = None
    description: str | None = None


@dataclass
class SetProjectIssueTypeScheme(Change):
    project_alias: str
    scheme_alias: str


@dataclass
class SetProjectIssueTypeScreenScheme(Change):
    project_alias: str
    scheme_alias: str


@dataclass
class SetProjectFieldConfigurationScheme(Change):
    project_alias: str
    scheme_alias: str


@dataclass
class SetProjectIssueSecurityScheme(Change):
    project_alias: str
    scheme_alias: str


@dataclass
class SetProjectPermissionScheme(Change):
    project_alias: str
    scheme_alias: str


# Workflows / workflow schemes
@dataclass(frozen=True)
class WorkflowTransitionSpec:
    """One declared transition inside a workflow change.

    ``restrict_to`` holders carry the *declared* values (role name, group
    name, email or accountId, permission key); ops resolve them to Jira's
    restrict-rule form (comma-separated roleIds/groupIds/permissionKeys)
    at apply time.
    """

    name: str
    from_statuses: tuple[str, ...]  # status names; empty = GLOBAL transition
    to_status: str  # status name
    restrict_to: tuple[tuple[str, str], ...] = ()  # (holder_type, declared parameter)
    description: str = ""


@dataclass
class CreateWorkflow(Change):
    alias: str
    name: str
    description: str
    statuses: tuple[tuple[str, str, str], ...]  # (name, category, description) declaration order
    initial: str  # status name
    transitions: tuple[WorkflowTransitionSpec, ...]  # declared order


@dataclass
class AdoptWorkflow(Change):
    """Record an existing (reflected) workflow into state without rewriting
    it. Adoption is non-destructive: undeclared transitions, validators and
    postfunctions stay; drift is reported by engine.check."""

    alias: str
    name: str


@dataclass
class UpdateWorkflow(Change):
    alias: str
    name: str
    statuses: tuple[tuple[str, str, str], ...]  # declared statuses (name, category, description)
    initial: str
    transitions: tuple[WorkflowTransitionSpec, ...]
    version_id: str = ""  # optimistic-locking DocumentVersion from reflect
    version_number: int = 0
    preserve_statuses: tuple[str, ...] = ()  # live status names kept (undeclared)
    preserve_transitions: tuple[dict[str, Any], ...] = ()  # raw reflected transitions kept


@dataclass
class DeleteWorkflow(Change):
    alias: str


@dataclass
class CreateWorkflowScheme(Change):
    alias: str
    name: str
    description: str
    default_workflow: str  # workflow alias
    mappings: tuple[tuple[str, str], ...]  # (issuetype alias, workflow alias)


@dataclass
class AdoptWorkflowScheme(Change):
    alias: str
    name: str


@dataclass
class UpdateWorkflowScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None
    default_workflow: str | None = None
    mappings: tuple[tuple[str, str], ...] | None = None  # full desired mapping set


@dataclass
class DeleteWorkflowScheme(Change):
    alias: str


@dataclass
class SetProjectWorkflowScheme(Change):
    """Bind (or rebind) a project's workflow scheme.

    ``live=False`` — the project is being created in this same migration
    (no issues yet): the sync associate endpoint runs.

    ``live=True`` — the project already exists: Jira runs an async switch
    task. ``status_mappings`` maps old status name → new status name for
    issues that would otherwise be stranded; Jira dictates *which*
    statuses require mapping (the op reads it and refuses to guess), and
    the operator fills the values in the migration before applying.
    """

    project_alias: str
    scheme_alias: str
    live: bool = True
    status_mappings: tuple[tuple[str, str], ...] = ()  # (old status name, new status name)


# Automation rules
@dataclass
class CreateAutomationRule(Change):
    alias: str
    name: str
    description: str
    payload: dict[str, Any]  # rule JSON, {{stint.*}} placeholders intact
    scope_projects: tuple[str, ...] = ()  # bound project keys (ARIs resolved at apply)
    refs: tuple[tuple[str, str, str], ...] = ()  # (ref name, kind, declared value)
    enabled: bool = True
    is_guard: bool = False
    allowed_writers: tuple[tuple[str, str], ...] = ()  # guards: (kind, declared value)
    actor: str | None = None  # declared actor email/accountId (None = A4J app actor)


@dataclass
class AdoptAutomationRule(Change):
    """Record an existing (reflected, exact-name match) rule into state
    without rewriting it; drift is reported by engine.check."""

    alias: str
    name: str


@dataclass
class UpdateAutomationRule(Change):
    alias: str
    name: str
    description: str  # the op's description param overrides the payload's
    payload: dict[str, Any]  # declared payload, placeholders intact
    scope_projects: tuple[str, str] | tuple[str, ...] = ()
    refs: tuple[tuple[str, str, str], ...] = ()
    enabled: bool = True
    is_guard: bool = False
    allowed_writers: tuple[tuple[str, str], ...] = ()
    actor: str | None = None


@dataclass
class SetAutomationRuleState(Change):
    alias: str
    enabled: bool


@dataclass
class SetAutomationRuleScope(Change):
    """Pin a rule's project scope to the scheme's bound projects (one
    multi-project scope, never copies). Project ids resolve at apply."""

    alias: str
    scope_projects: tuple[str, ...] = ()  # project keys


@dataclass
class DeleteAutomationRule(Change):
    """Delete an undeclared rule: disable first (the API refuses enabled
    rules), then delete. Behind ``--allow-delete`` like every destructive
    op."""

    alias: str


# Project role actors
@dataclass
class AddProjectRoleActor(Change):
    project_alias: str
    role_name: str
    actor_type: str  # "user" or "group"
    actor_value: str  # declared email/accountId/group name (resolved at apply time)


@dataclass
class RemoveProjectRoleActor(Change):
    project_alias: str
    role_name: str
    actor_type: str  # "user" or "group"
    actor_parameter: str  # resolved accountId/group name (from reflection)


# Permission schemes
@dataclass
class CreatePermissionScheme(Change):
    alias: str
    name: str
    description: str
    copy_from: str | None
    grants: tuple[tuple[str, str, str], ...]  # (permission, holder_type, declared parameter)


@dataclass
class UpdatePermissionScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None


@dataclass
class AddPermissionGrant(Change):
    scheme_alias: str
    permission: str
    holder_type: str
    holder_parameter: str  # declared value; resolved at apply time


@dataclass
class RemovePermissionGrant(Change):
    scheme_alias: str
    permission: str
    holder_type: str
    holder_parameter: str  # resolved value (from reflection)


@dataclass
class DeletePermissionScheme(Change):
    alias: str


# Issue security schemes
@dataclass
class CreateIssueSecurityScheme(Change):
    alias: str
    name: str
    description: str
    levels: tuple[tuple[str, str, str], ...]  # (alias, name, description), declaration order
    default_level: str | None  # level alias; applied inline as isDefault on create


@dataclass
class UpdateIssueSecurityScheme(Change):
    alias: str
    name: str | None = None
    description: str | None = None


@dataclass
class AddSecurityLevel(Change):
    scheme_alias: str
    level_alias: str
    level_name: str
    description: str = ""
    is_default: bool = False


@dataclass
class AddSecurityLevelMember(Change):
    scheme_alias: str
    level_alias: str
    member_type: str  # "user" or "group"
    member_value: str  # group name, user email, or raw accountId


@dataclass
class SetDefaultSecurityLevel(Change):
    scheme_alias: str
    level_alias: str


@dataclass
class ClearDefaultSecurityLevel(Change):
    """Reset a scheme's default level to *none*.

    Jira then creates issues with no issue-security level, which — unlike a
    level with no members — is visible to everyone who can browse the
    project. Needed because "remove the default" has no schema spelling
    other than ``default_level=None``; before this, a declared default could
    be set but never unset, so every new issue stayed locked behind it.
    """

    scheme_alias: str


@dataclass
class DeleteProject(Change):
    alias: str
    key: str


# ── Diff result ──────────────────────────────────────────────────────
@dataclass
class DiffResult:
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.changes)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def __iter__(self):
        return iter(self.changes)


# ── Top-level diff ───────────────────────────────────────────────────
def diff(
    *,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool = False,
    member_params: dict[str, str] | None = None,
) -> DiffResult:
    """Return ordered Change list + warnings. Phase order applied by emit.

    `allow_delete` is required for any destructive op (delete_*, remove_option).
    Destructive ops are silently dropped (with a warning) when False.

    `member_params` (optional) maps declared security level member values to
    their resolved ``holder.parameter`` form (declared user email →
    accountId). The caller resolves them where the dialect is reachable;
    the diff itself stays sync so it can be exercised without HTTP.

    Role actors and permission-scheme grant holders declared as user emails
    use the same map. Project-role grant holders are resolved against the
    snapshot's global role-name → id map inside the diff (no HTTP needed).
    """
    r = DiffResult()

    _diff_custom_fields(r, desired, snapshot, state, allow_delete)
    _diff_issuetypes(r, desired, snapshot, state, allow_delete)
    _diff_field_configurations(r, desired, snapshot, state, allow_delete)
    _diff_screens(r, desired, snapshot, state, allow_delete)
    _diff_screen_schemes(r, desired, snapshot, state, allow_delete)
    _diff_its(r, desired, snapshot, state, allow_delete)
    _diff_itss(r, desired, snapshot, state, allow_delete)
    _diff_fcs(r, desired, snapshot, state, allow_delete)
    _diff_projects(r, desired, snapshot, state, allow_delete)
    _diff_security_schemes(r, desired, snapshot, state, allow_delete, member_params=member_params)
    _diff_permission_schemes(r, desired, snapshot, state, allow_delete, member_params=member_params)
    _diff_project_roles(r, desired, snapshot, state, allow_delete, member_params=member_params)
    _diff_workflows(r, desired, snapshot, state, allow_delete, member_params=member_params)
    _diff_workflow_schemes(r, desired, snapshot, state, allow_delete)
    _diff_automation_rules(r, desired, snapshot, state, allow_delete, member_params=member_params)
    return r


# ── Custom fields ────────────────────────────────────────────────────
def _diff_custom_fields(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    """Diff desired custom fields against reflection.

    Field comments: ``POST /field`` ignores the ``description`` parameter
    (only name/type/searcherKey), so the create op applies the description
    via the documented ``PUT /field/{id}`` right after creating; that makes
    fresh fields match the schema immediately, no phantom re-emission."""
    for alias, want in desired.custom_fields.items():
        if alias not in state.custom_fields:
            r.changes.append(
                CreateCustomField(
                    alias=alias,
                    name=want.name,
                    type_id=want.type_id,
                    description=want.description,
                    options=want.options,
                    searcher_key=want.searcher_key,
                )
            )
            continue
        existing_id = state.custom_fields[alias].id
        actual = snapshot.custom_fields.get(existing_id)
        if actual is None:
            r.warnings.append(
                f"custom_field {alias!r}: state references id {existing_id!r} "
                f"but it's not present in Jira (out-of-band delete?)"
            )
            continue
        # Attribute diff
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        # A field with no searcher (search template None) can be written and
        # read but never used in JQL. Repair it when the reflected value is
        # missing or different. Only compare when the declaration has one, so
        # an unknown type never emits a no-op churn update.
        searcher_change = (
            want.searcher_key
            if want.searcher_key and actual.searcher_key is not None and actual.searcher_key != want.searcher_key
            else None
        )
        if name_change is not None or desc_change is not None or searcher_change is not None:
            r.changes.append(
                UpdateCustomField(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                    searcher_key=searcher_change,
                )
            )
        # Option diff (additions / removals)
        actual_opts = set(actual.options)
        want_opts = set(want.options)
        for new_val in (v for v in want.options if v not in actual_opts):
            r.changes.append(AddCustomFieldOption(field_alias=alias, value=new_val))
        for old_val in sorted(actual_opts - want_opts):
            if allow_delete:
                r.changes.append(
                    RemoveCustomFieldOption(
                        field_alias=alias,
                        value=old_val,
                    )
                )
            else:
                r.warnings.append(
                    f"custom_field {alias!r}: option {old_val!r} is in Jira but "
                    f"not in schema. Re-run with --allow-delete to remove."
                )

    if allow_delete:
        for alias in sorted(state.custom_fields):
            if alias not in desired.custom_fields:
                r.changes.append(DeleteCustomField(alias=alias))
    else:
        _warn_orphans(
            r,
            "custom_field",
            state.custom_fields,
            desired.custom_fields,
        )


# ── Issue types ──────────────────────────────────────────────────────
def _diff_issuetypes(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.issuetypes.items():
        if alias not in state.issuetypes:
            r.changes.append(
                CreateIssueType(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    subtask=want.subtask,
                )
            )
            continue
        existing_id = state.issuetypes[alias].id
        actual = snapshot.issuetypes.get(existing_id)
        if actual is None:
            r.warnings.append(f"issuetype {alias!r}: state references id {existing_id!r} but it's not present in Jira")
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        if name_change is not None or desc_change is not None:
            r.changes.append(
                UpdateIssueType(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.issuetypes):
            if alias not in desired.issuetypes:
                r.changes.append(DeleteIssueType(alias=alias))
    else:
        _warn_orphans(r, "issuetype", state.issuetypes, desired.issuetypes)


# ── Screens ──────────────────────────────────────────────────────────
def _diff_screens(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.screens.items():
        if alias not in state.screens:
            r.changes.append(
                CreateScreen(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                )
            )
            # Always one tab "Fields" in 0.1
            tab = want.tabs[0]
            r.changes.append(AddScreenTab(screen_alias=alias, tab_name=tab.name))
            for field_ref in tab.field_refs:
                r.changes.append(
                    AddScreenTabField(
                        screen_alias=alias,
                        tab_name=tab.name,
                        field_alias=field_ref,
                    )
                )
            continue
        existing_id = state.screens[alias].id
        actual = snapshot.screens.get(existing_id)
        if actual is None:
            r.warnings.append(f"screen {alias!r}: state references id {existing_id!r} but it's not present in Jira")
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        if name_change is not None or desc_change is not None:
            r.changes.append(
                UpdateScreen(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                )
            )
        # L21: tab/field membership of existing screens used to be a silent
        # gap ("no changes detected" while members had drifted). Op-level
        # membership updates are still not emitted (op surface has no
        # remove-reorder path), but checkable drift must not go unreported:
        # warn naming every declared field absent from the reflected screen.
        missing = _missing_screen_fields(want, actual, state)
        if missing:
            r.warnings.append(
                f"screen {alias!r}: declared field(s) {', '.join(missing)} "
                f"are not on the reflected screen in Jira; membership changes "
                f"are not emitted — add them in the Jira UI, delete and "
                f"recreate the screen, or edit its migration by hand"
            )

    if allow_delete:
        for alias in sorted(state.screens):
            if alias not in desired.screens:
                r.changes.append(DeleteScreen(alias=alias))
    else:
        _warn_orphans(r, "screen", state.screens, desired.screens)


# ── Screen schemes ───────────────────────────────────────────────────
def _diff_screen_schemes(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.screen_schemes.items():
        if alias not in state.screen_schemes:
            r.changes.append(
                CreateScreenScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    screens=dict(want.screens),
                )
            )
            continue
        existing_id = state.screen_schemes[alias].id
        actual = snapshot.screen_schemes.get(existing_id)
        if actual is None:
            r.warnings.append(
                f"screen_scheme {alias!r}: state references id {existing_id!r} but it's not present in Jira"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        # Resolve want.screens (op → screen alias) to Jira IDs for comparison
        want_screens_ids: dict[str, str] = {}
        for screen_op, screen_alias in want.screens.items():
            screen_mapping = state.screens.get(screen_alias)
            if screen_mapping is not None:
                want_screens_ids[screen_op] = screen_mapping.id
            # else: schema references a screen not in state — handled by
            # the screen's own diff (will be created in this same migration).
        screens_change = dict(want.screens) if want_screens_ids != actual.mappings else None
        if name_change is not None or desc_change is not None or screens_change is not None:
            r.changes.append(
                UpdateScreenScheme(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                    screens=screens_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.screen_schemes):
            if alias not in desired.screen_schemes:
                r.changes.append(DeleteScreenScheme(alias=alias))
    else:
        _warn_orphans(r, "screen_scheme", state.screen_schemes, desired.screen_schemes)


# ── IssueTypeScheme ──────────────────────────────────────────────────
def _diff_its(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.issuetype_schemes.items():
        if alias not in state.issuetype_schemes:
            r.changes.append(
                CreateIssueTypeScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    issuetypes=want.issuetypes,
                    default_issuetype=want.default_issuetype,
                )
            )
            continue
        existing_id = state.issuetype_schemes[alias].id
        actual = snapshot.issuetype_schemes.get(existing_id)
        if actual is None:
            r.warnings.append(
                f"issuetype_scheme {alias!r}: state references id {existing_id!r} but it's not present in Jira"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        # Compare desired issuetype aliases (resolved to ids via state) against actual ids.
        desired_ids: list[str] = []
        for it_alias in want.issuetypes:
            m = state.issuetypes.get(it_alias)
            if m is None:
                # Member is a desired issuetype that hasn't been stamped/created yet —
                # the diff for that issuetype runs earlier in the same migration. Skip
                # the list-level compare; the create-side change is what matters here.
                desired_ids = []
                break
            desired_ids.append(m.id)
        members_change: tuple[str, ...] | None = None
        if desired_ids and tuple(desired_ids) != tuple(actual.issuetype_ids):
            members_change = want.issuetypes
        default_m = state.issuetypes.get(want.default_issuetype)
        default_change: str | None = None
        if default_m is not None and actual.default_issuetype_id != default_m.id:
            default_change = want.default_issuetype
        if any(c is not None for c in (name_change, desc_change, members_change, default_change)):
            r.changes.append(
                UpdateIssueTypeScheme(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                    issuetypes=members_change,
                    default_issuetype=default_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.issuetype_schemes):
            if alias not in desired.issuetype_schemes:
                r.changes.append(DeleteIssueTypeScheme(alias=alias))
    else:
        _warn_orphans(
            r,
            "issuetype_scheme",
            state.issuetype_schemes,
            desired.issuetype_schemes,
        )


# ── ITSS ─────────────────────────────────────────────────────────────
def _diff_itss(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.issuetype_screen_schemes.items():
        if alias not in state.issuetype_screen_schemes:
            r.changes.append(
                CreateIssueTypeScreenScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    mappings=dict(want.mappings),
                )
            )
            continue
        existing_id = state.issuetype_screen_schemes[alias].id
        actual = snapshot.issuetype_screen_schemes.get(existing_id)
        if actual is None:
            r.warnings.append(
                f"issuetype_screen_scheme {alias!r}: state references id {existing_id!r} but it's not present in Jira"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        mappings_change = _itss_mappings_changed(want, actual, state)
        if name_change is not None or desc_change is not None or mappings_change is not None:
            r.changes.append(
                UpdateIssueTypeScreenScheme(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                    mappings=mappings_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.issuetype_screen_schemes):
            if alias not in desired.issuetype_screen_schemes:
                r.changes.append(DeleteIssueTypeScreenScheme(alias=alias))
    else:
        _warn_orphans(
            r,
            "issuetype_screen_scheme",
            state.issuetype_screen_schemes,
            desired.issuetype_screen_schemes,
        )


# ── FCS ──────────────────────────────────────────────────────────────
def _diff_fcs(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.field_configuration_schemes.items():
        if alias not in state.field_configuration_schemes:
            r.changes.append(
                CreateFieldConfigurationScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    mappings=dict(want.mappings),
                )
            )
            continue
        existing_id = state.field_configuration_schemes[alias].id
        actual = snapshot.field_configuration_schemes.get(existing_id)
        if actual is None:
            r.warnings.append(
                f"field_configuration_scheme {alias!r}: state references id "
                f"{existing_id!r} but it's not present in Jira"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        mappings_change = _fcs_mappings_changed(want, actual, state)
        if name_change is not None or desc_change is not None or mappings_change is not None:
            r.changes.append(
                UpdateFieldConfigurationScheme(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                    mappings=mappings_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.field_configuration_schemes):
            if alias not in desired.field_configuration_schemes:
                r.changes.append(DeleteFieldConfigurationScheme(alias=alias))
    else:
        _warn_orphans(
            r,
            "field_configuration_scheme",
            state.field_configuration_schemes,
            desired.field_configuration_schemes,
        )


# ── Field configurations ─────────────────────────────────────────────
def _diff_field_configurations(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.field_configurations.items():
        if alias not in state.field_configurations:
            r.changes.append(
                CreateFieldConfiguration(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                )
            )
            for item in want.items:
                r.changes.append(
                    SetFieldConfigurationItem(
                        fc_alias=alias,
                        field_alias=item.field_alias,
                        required=item.required,
                        hidden=item.hidden,
                    )
                )
            continue
        # Existing FC — name/desc diff. Item changes are not diffed in 0.1
        # (would need to round-trip field_id → alias which depends on state).
        existing_id = state.field_configurations[alias].id
        actual = snapshot.field_configurations.get(existing_id)
        if actual is None:
            r.warnings.append(
                f"field_configuration {alias!r}: state references id {existing_id!r} but it's not present in Jira"
            )
            continue
        # L21: FC item changes of existing configurations used to be a
        # silent gap. Op-level item updates for existing FCs still need an
        # alias→field_id round-trip, but checkable drift must be reported:
        # required/hidden flags and missing declared items.
        item_drift = _field_configuration_item_drift(want, actual, state)
        if item_drift:
            r.warnings.append(
                f"field_configuration {alias!r}: {item_drift}; item changes "
                f"on existing configurations are not emitted — set them in "
                f"the Jira UI or edit the migration by hand"
            )
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        if name_change is not None or desc_change is not None:
            r.changes.append(
                UpdateFieldConfiguration(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.field_configurations):
            if alias not in desired.field_configurations:
                r.changes.append(DeleteFieldConfiguration(alias=alias))
    else:
        _warn_orphans(
            r,
            "field_configuration",
            state.field_configurations,
            desired.field_configurations,
        )


# ── Projects ─────────────────────────────────────────────────────────
def _diff_projects(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    for alias, want in desired.projects.items():
        if alias not in state.projects:
            r.changes.append(
                CreateProject(
                    alias=alias,
                    key=want.key,
                    name=want.name,
                    project_type_key=want.project_type_key,
                    lead=want.lead,
                    description=want.description,
                    lead_role=want.lead_role,
                )
            )
            if want.issuetype_scheme is not None:
                r.changes.append(
                    SetProjectIssueTypeScheme(
                        project_alias=alias,
                        scheme_alias=want.issuetype_scheme,
                    )
                )
            if want.issuetype_screen_scheme is not None:
                r.changes.append(
                    SetProjectIssueTypeScreenScheme(
                        project_alias=alias,
                        scheme_alias=want.issuetype_screen_scheme,
                    )
                )
            if want.field_configuration_scheme is not None:
                r.changes.append(
                    SetProjectFieldConfigurationScheme(
                        project_alias=alias,
                        scheme_alias=want.field_configuration_scheme,
                    )
                )
            if want.security_scheme is not None:
                r.changes.append(
                    SetProjectIssueSecurityScheme(
                        project_alias=alias,
                        scheme_alias=want.security_scheme,
                    )
                )
            if want.permission_scheme is not None:
                r.changes.append(
                    SetProjectPermissionScheme(
                        project_alias=alias,
                        scheme_alias=want.permission_scheme,
                    )
                )
            if want.workflow_scheme is not None:
                r.changes.append(
                    SetProjectWorkflowScheme(
                        project_alias=alias,
                        scheme_alias=want.workflow_scheme,
                        live=False,
                    )
                )
            continue
        # Existing project — name/lead/description diff.
        existing_id = state.projects[alias].id
        actual = _find_project_by_id(snapshot, existing_id)
        if actual is None:
            r.warnings.append(f"project {alias!r}: state references id {existing_id!r} but it's not present in Jira")
            continue
        # Style mismatch is informational only — Jira does not allow style
        # conversion via REST. The schema needs to align with Jira, or the
        # project needs to be recreated by hand.
        existing_style = state.projects[alias].style
        if existing_style and existing_style != want.style:
            r.warnings.append(
                f"project {alias!r}: schema declares {want.style!r} but state "
                f"records {existing_style!r}. Jira does not support style "
                f"conversion via REST; align the schema or recreate the project."
            )
        name_change = want.name if actual.name != want.name else None
        # Lead drift is intentionally NOT auto-detected on existing projects.
        # The schema declares __lead__ as an email, but the reflected snapshot
        # reports the backend identity (Jira Cloud accountId).
        # The two are never directly comparable, so comparing them would emit an
        # UpdateProject lead change on every autogenerate run forever. Lead is
        # still applied on project creation; change an existing project's lead
        # with a hand-written migration. (#7 follow-up)
        if name_change is not None:
            r.changes.append(UpdateProject(alias=alias, name=name_change))
        # L21: description drift used to be silently undetectable because
        # ProjectSnapshot carried no description; /project/search provides
        # it now. Team-managed snapshots leave description "" (lives in the
        # work-type, not PUT /project), so skips there.
        if actual.description != want.description and state.projects[alias].style != "team-managed":
            r.changes.append(UpdateProject(alias=alias, description=want.description))
        # Scheme rebinds when the project's actual binding drifts from desired.
        # Skip when the desired scheme alias isn't yet in state — the create-
        # side change emits the binding for newly-created schemes.
        _maybe_emit_set_scheme(
            r,
            project_alias=alias,
            desired_scheme=want.issuetype_scheme,
            state_table=state.issuetype_schemes,
            actual_id=actual.issuetype_scheme_id,
            change_cls=SetProjectIssueTypeScheme,
        )
        _maybe_emit_set_scheme(
            r,
            project_alias=alias,
            desired_scheme=want.issuetype_screen_scheme,
            state_table=state.issuetype_screen_schemes,
            actual_id=actual.issuetype_screen_scheme_id,
            change_cls=SetProjectIssueTypeScreenScheme,
        )
        _maybe_emit_set_scheme(
            r,
            project_alias=alias,
            desired_scheme=want.field_configuration_scheme,
            state_table=state.field_configuration_schemes,
            actual_id=actual.field_configuration_scheme_id,
            change_cls=SetProjectFieldConfigurationScheme,
        )
        _maybe_emit_set_scheme(
            r,
            project_alias=alias,
            desired_scheme=want.security_scheme,
            state_table=state.security_schemes,
            actual_id=actual.issue_security_scheme_id,
            change_cls=SetProjectIssueSecurityScheme,
        )
        _maybe_emit_set_scheme(
            r,
            project_alias=alias,
            desired_scheme=want.permission_scheme,
            state_table=state.permission_schemes,
            actual_id=actual.permission_scheme_id,
            change_cls=SetProjectPermissionScheme,
        )
        _maybe_emit_set_workflow_scheme(
            r,
            project_alias=alias,
            desired_scheme=want.workflow_scheme or "",
            state=state,
            actual_scheme_id=actual.workflow_scheme_id,
            project_in_state=True,
        )

    if allow_delete:
        for alias in sorted(state.projects):
            if alias not in desired.projects:
                # Need the key to render delete_project; pull from snapshot if known.
                key = _resolve_project_key(alias, state, snapshot) or alias
                r.changes.append(DeleteProject(alias=alias, key=key))
    else:
        _warn_orphans(r, "project", state.projects, desired.projects)


# ── Issue security schemes ───────────────────────────────────────────
def _diff_security_schemes(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
    member_params: dict[str, str] | None = None,
) -> None:
    for alias, want in desired.security_schemes.items():
        if want.project is not None and want.project not in desired.projects:
            r.warnings.append(
                f"security_scheme {alias!r}: link target project {want.project!r} "
                f"is not a declared Project key; the associate op will fail until "
                f"the schema declares it (Project alias is authoritative)."
            )
        if alias not in state.security_schemes:
            r.changes.append(
                CreateIssueSecurityScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    levels=tuple((lv.alias, lv.name, lv.description) for lv in want.levels),
                    default_level=want.default_level,
                )
            )
            # The create endpoint takes no member list (emails resolve
            # per-member at apply time), so declared members ride along as
            # phase-45 AddSecurityLevelMember ops: after the create captures
            # level ids (42), before the project binding (62). Without these,
            # the first migration creates member-less levels — a project whose
            # default level has no members hides every new issue from
            # everyone, including the creating token, until a second
            # revision/upgrade runs.
            for level in want.levels:
                for member in level.members:
                    r.changes.append(
                        AddSecurityLevelMember(
                            scheme_alias=alias,
                            level_alias=level.alias,
                            member_type=member.type,
                            member_value=member.value,
                        )
                    )
            # The binding for an existing project fires on the project side
            # (`_diff_projects` → _maybe_emit_set_scheme), which covers both
            # a pending scheme (phase-ordered create → bind) and a drifted
            # one; a newly-created project binds on its own create path.
            continue
        mapping = state.security_schemes[alias]
        actual = snapshot.security_schemes.get(mapping.id)
        if actual is None:
            r.warnings.append(
                f"security_scheme {alias!r}: state references id {mapping.id!r} "
                f"but it's not present in Jira (out-of-band delete?)"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        if name_change is not None or desc_change is not None:
            r.changes.append(UpdateIssueSecurityScheme(alias=alias, name=name_change, description=desc_change))
        # Level diff (adds only; level/member removal is destructive and is
        # hand-written in a migration).
        actual_by_id = {lv.id: lv for lv in actual.levels}
        actual_by_name = {lv.name: lv for lv in actual.levels}
        for level in want.levels:
            want_id = mapping.level_ids.get(level.alias)
            found = actual_by_id.get(want_id) if want_id else None
            if found is None:
                # State has no stored id (fresh create) or it drifted: match
                # by name, which Jira keeps unique within a scheme.
                found = actual_by_name.get(level.name)
            if found is None:
                r.changes.append(
                    AddSecurityLevel(
                        scheme_alias=alias,
                        level_alias=level.alias,
                        level_name=level.name,
                        description=level.description,
                        is_default=level.is_default,
                    )
                )
                continue
            # Member adds against an existing level. Recorded member_values
            # are the resolved parameters the op already sent (idempotency
            # anchor for email→accountId resolution); reflection supplies
            # holder.parameter for everything else.
            recorded = set(mapping.member_values.get(level.alias, []))
            # Compare on the (parameter, type) pair Jira's "already exists"
            # dedupe actually keys on — a parameter-only match lets a
            # projectRoles member mask a declared user/group member.
            actual_members = {(m.parameter, m.type) for m in found.members if m.parameter}
            for member in level.members:
                # The canonical form is what both state and reflection carry:
                # a user declared as an email (the schema convention) arrives
                # here as an accountId everywhere else. Comparing the raw
                # declared value against it never matches, so the diff used
                # to re-emit the member add on every revision and the upgrade
                # then failed with Jira's "already exists" 400.
                canonical = (member_params or {}).get(member.value, member.value)
                if member.value in recorded or canonical in recorded:
                    continue
                if (canonical, member.type) in actual_members or (member.value, member.type) in actual_members:
                    continue
                r.changes.append(
                    AddSecurityLevelMember(
                        scheme_alias=alias,
                        level_alias=level.alias,
                        member_type=member.type,
                        member_value=member.value,
                    )
                )
        # Default-level drift on an existing scheme.
        _diff_security_default_level(r, want, actual, mapping)

    if allow_delete:
        for alias in sorted(state.security_schemes):
            if alias not in desired.security_schemes:
                r.changes.append(DeleteIssueSecurityScheme(alias=alias))
    else:
        _warn_orphans(r, "security_scheme", state.security_schemes, desired.security_schemes)


def _diff_security_default_level(
    r: DiffResult,
    want: Any,
    actual: Any,
    mapping: Any,
) -> None:
    """Emit Set/ClearDefaultSecurityLevel when the default drifts.

    The desired designation resolves through stored level ids first (exact
    once-applied reality), then through the reflected scheme's level names —
    a fresh scheme's ids aren't in state until the create op captures them,
    and name matching (unique per scheme) substitutes safely.

    ``default_level=None`` means "no default": new issues get no security
    level and are visible to anyone who can browse the project. A live
    default left behind from an earlier schema (or set in the Jira UI) must
    be cleared explicitly — leaving it makes every issue the data plane
    creates invisible to JQL and to ``GET /issue`` (live tenant, 2026-11).
    """
    if not actual.levels:
        return
    if want.default_level is None:
        if actual.default_security_level_id is not None:
            r.changes.append(ClearDefaultSecurityLevel(scheme_alias=want.alias))
        return
    want_level_id = mapping.level_ids.get(want.default_level)
    if want_level_id is None:
        match = next((lv for lv in actual.levels if lv.name.lower() == want.default_level.lower()), None)
        want_level_id = match.id if match else None
    if not want_level_id or actual.default_security_level_id == want_level_id:
        return
    r.changes.append(SetDefaultSecurityLevel(scheme_alias=want.alias, level_alias=want.default_level))


# ── Permission schemes ───────────────────────────────────────────────
def _diff_permission_schemes(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
    member_params: dict[str, str] | None = None,
) -> None:
    """Diff desired permission schemes against reflection.

    Grant policy (explicit, documented): declared grants are always managed
    (added when missing). Live grants that are NOT declared are removed only
    when the scheme has no ``copy_from`` (exact policy, ``--allow-delete``
    gated); with ``copy_from`` they are inherited/unmanaged and only
    ``engine.check`` reports them.
    """
    for alias, want in desired.permission_schemes.items():
        if want.project is not None and want.project not in desired.projects:
            r.warnings.append(
                f"permission_scheme {alias!r}: link target project {want.project!r} "
                f"is not a declared Project key; the associate op will fail until "
                f"the schema declares it (Project alias is authoritative)."
            )
        if alias not in state.permission_schemes:
            r.changes.append(
                CreatePermissionScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    copy_from=want.copy_from,
                    grants=tuple((g.permission, g.holder_type, g.holder_parameter) for g in want.grants),
                )
            )
            continue
        mapping = state.permission_schemes[alias]
        actual = snapshot.permission_schemes.get(mapping.id)
        if actual is None:
            r.warnings.append(
                f"permission_scheme {alias!r}: state references id {mapping.id!r} "
                f"but it's not present in Jira (out-of-band delete?)"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        if name_change is not None or desc_change is not None:
            r.changes.append(UpdatePermissionScheme(alias=alias, name=name_change, description=desc_change))
        actual_grants = {(g.permission, g.holder_type, g.holder_parameter): g.id for g in actual.grants}
        recorded = {(g["permission"], g["type"], g["parameter"]) for g in mapping.grants}
        declared_resolved: set[tuple[str, str, str]] = set()
        for grant in want.grants:
            resolved = _resolve_grant_parameter(grant.holder_type, grant.holder_parameter, member_params, snapshot)
            if grant.holder_type == "projectrole" and resolved is None:
                # Role name unresolved against the reflected global role map.
                # Still emit the add: the op resolves per project at apply
                # time (role ids are tenant-global), and a wrong name fails
                # there with a clear error instead of here.
                r.warnings.append(
                    f"permission_scheme {alias!r}: grant holder project role "
                    f"{grant.holder_parameter!r} is not in the reflected role map; "
                    f"emitting the grant anyway (the apply-time role lookup will "
                    f"fail loudly if the role does not exist)."
                )
                resolved = grant.holder_parameter
            declared_resolved.add((grant.permission, grant.holder_type, resolved or ""))
            if (grant.permission, grant.holder_type, resolved) in actual_grants:
                continue
            if (grant.permission, grant.holder_type, resolved) in recorded:
                continue
            r.changes.append(
                AddPermissionGrant(
                    scheme_alias=alias,
                    permission=grant.permission,
                    holder_type=grant.holder_type,
                    holder_parameter=grant.holder_parameter,
                )
            )
        if want.copy_from is None:
            # Exact policy: live grants that are not declared are drift.
            for (permission, holder_type, holder_parameter), _grant_id in sorted(actual_grants.items()):
                if (permission, holder_type, holder_parameter) in declared_resolved:
                    continue
                if allow_delete:
                    r.changes.append(
                        RemovePermissionGrant(
                            scheme_alias=alias,
                            permission=permission,
                            holder_type=holder_type,
                            holder_parameter=holder_parameter,
                        )
                    )
                else:
                    r.warnings.append(
                        f"permission_scheme {alias!r}: grant {permission} to "
                        f"{holder_type} {holder_parameter!r} is in Jira but not in "
                        f"schema. Re-run with --allow-delete to remove."
                    )

    if allow_delete:
        for alias in sorted(state.permission_schemes):
            if alias not in desired.permission_schemes:
                r.changes.append(DeletePermissionScheme(alias=alias))
    else:
        _warn_orphans(r, "permission_scheme", state.permission_schemes, desired.permission_schemes)


def _resolve_grant_parameter(
    holder_type: str,
    holder_parameter: str,
    member_params: dict[str, str] | None,
    snapshot: Snapshot,
) -> str:
    """Resolve a declared grant holder parameter to Jira's holder form.

    Users: email → accountId via ``member_params`` (fallback: the declared
    value, assumed already-resolved). projectRole: role name → role id via
    the snapshot's global role map; returns None when unresolved. Everything
    else passes through.
    """
    if holder_type == "user":
        return (member_params or {}).get(holder_parameter, holder_parameter)
    if holder_type.lower() == "projectrole":
        return snapshot.project_roles.get(holder_parameter) or ""
    return holder_parameter


# ── Project role actors ──────────────────────────────────────────────
def _diff_project_roles(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
    member_params: dict[str, str] | None = None,
) -> None:
    """Diff declared ``__roles__`` memberships against reflection.

    Roles that are not declared are left alone (unmanaged), so adopting
    stint on an existing project never wipes its memberships. Within a
    declared role, actors absent from the declaration are removed — gated
    behind ``allow_delete`` like every destructive change.
    """
    for alias, want in desired.projects.items():
        if not want.roles:
            continue
        if alias not in state.projects:
            # The project is being created in this same migration: only
            # adds exist (phase-ordered after CreateProject).
            for role in want.roles:
                for actor_type, actor_value in role.actors:
                    r.changes.append(
                        AddProjectRoleActor(
                            project_alias=alias,
                            role_name=role.role_name,
                            actor_type=actor_type,
                            actor_value=actor_value,
                        )
                    )
            continue
        existing_id = state.projects[alias].id
        actual = _find_project_by_id(snapshot, existing_id)
        if actual is None:
            r.warnings.append(f"project {alias!r}: state references id {existing_id!r} but it's not present in Jira")
            continue
        for role in want.roles:
            actual_actors = {(a.type, a.parameter) for a in (actual.roles.get(role.role_name) or ()) if a.parameter}
            if not actual_actors and role.role_name not in actual.roles:
                r.warnings.append(
                    f"project {alias!r}: declared role {role.role_name!r} was not found "
                    f"on the project in Jira; the actor add(s) will fail at apply time "
                    f"unless the role exists (Jira templates create a default role set)."
                )
            declared: set[tuple[str, str]] = set()
            for actor_type, actor_value in role.actors:
                resolved = actor_value
                if actor_type == "user":
                    resolved = (member_params or {}).get(actor_value, actor_value)
                declared.add((actor_type, resolved))
                if (actor_type, resolved) not in actual_actors:
                    r.changes.append(
                        AddProjectRoleActor(
                            project_alias=alias,
                            role_name=role.role_name,
                            actor_type=actor_type,
                            actor_value=actor_value,
                        )
                    )
            for actor_type, parameter in sorted(actual_actors - declared):
                if allow_delete:
                    r.changes.append(
                        RemoveProjectRoleActor(
                            project_alias=alias,
                            role_name=role.role_name,
                            actor_type=actor_type,
                            actor_parameter=parameter,
                        )
                    )
                else:
                    r.warnings.append(
                        f"project {alias!r}: role {role.role_name!r} actor "
                        f"{actor_type} {parameter!r} is in Jira but not in the "
                        f"declared __roles__. Re-run with --allow-delete to remove."
                    )


def _resolve_project_key(
    alias: str,
    state: StateFile,
    snapshot: Snapshot,
) -> str | None:
    project_id = state.projects[alias].id
    for proj in snapshot.projects.values():
        if proj.id == project_id:
            return proj.key
    return None


# ── Workflows / workflow schemes ─────────────────────────────────────
def _declared_transition_specs(want) -> tuple[WorkflowTransitionSpec, ...]:
    return tuple(
        WorkflowTransitionSpec(
            name=t.name,
            from_statuses=t.from_statuses,
            to_status=t.to_status,
            restrict_to=tuple((h.holder_type, h.holder_parameter) for h in t.restrict_to),
            description=t.description,
        )
        for t in want.transitions
    )


def _restrict_comparable(
    actual_params: dict[str, str] | None,
    declared: tuple[tuple[str, str], ...],
    snapshot: Snapshot,
) -> bool | None:
    """Three-way comparison of declared restrict holders against a live
    transition's restrict parameters.

    True = provably equal, False = provably different, None = unprovable
    (group-name → group-id resolution needs HTTP; the diff is sync). Only
    ``permissionKeys`` and ``roleIds`` (resolvable against the reflected
    global role map) are comparable here; any declared group holder or an
    unresolvable role name makes the comparison unprovable, which the
    engine.check layer reports as drift rather than the diff rewriting.
    """
    declared_keys = sorted(p for t, p in declared if t == "permission")
    declared_role_names = [p for t, p in declared if t == "projectrole"]
    if declared_role_names:
        declared_roles: list[str] | None = []
        for name in declared_role_names:
            role_id = snapshot.project_roles.get(name)
            if role_id is None:
                return None  # unresolvable role name
            declared_roles.append(role_id)
    else:
        declared_roles = []
    if any(t in ("group", "user") for t, _ in declared):
        return None
    if actual_params is None:
        return not declared_keys and not declared_roles
    actual_keys = sorted({v.strip() for v in str(actual_params.get("permissionKeys", "")).split(",") if v.strip()})
    actual_roles = sorted({v.strip() for v in str(actual_params.get("roleIds", "")).split(",") if v.strip()})
    return declared_keys == actual_keys and sorted(declared_roles or []) == actual_roles


def _diff_workflows(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
    member_params: dict[str, str] | None = None,
) -> None:
    """Diff declared workflows against reflection, keyed by *name* (the
    handle the classic scheme APIs and operators use; the new-model UUID is
    bookkeeping recorded in state).

    Adoption (name exists in Jira, alias not in state) is non-destructive:
    an AdoptWorkflow records the mapping without rewriting — undeclared
    transitions/validators/postfunctions stay, and drift is reported by
    engine.check. Once adopted, declared-content drift emits an
    UpdateWorkflow whose payload preserves every undeclared live status and
    transition (verbatims ride along so the declarative update does not
    delete them — Jira's update replace semantics are undocumented).
    """
    for alias, want in desired.workflows.items():
        if alias not in state.workflows:
            existing = snapshot.workflows.get(want.name)
            if existing is not None and existing.scope == "GLOBAL":
                r.changes.append(AdoptWorkflow(alias=alias, name=want.name))
                continue
            if existing is not None:
                r.warnings.append(
                    f"workflow {alias!r}: a project-scoped (team-managed) workflow named "
                    f"{want.name!r} exists; company-managed declarations cannot manage it."
                )
            r.changes.append(
                CreateWorkflow(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    statuses=tuple((s.name, s.category, s.description) for s in want.statuses),
                    initial=want.initial,
                    transitions=_declared_transition_specs(want),
                )
            )
            continue
        mapping = state.workflows[alias]
        actual = snapshot.workflows.get(want.name) or next(
            (w for w in snapshot.workflows.values() if w.id == mapping.id), None
        )
        if actual is None:
            r.warnings.append(
                f"workflow {alias!r}: state references id {mapping.id!r} but no workflow "
                f"named {want.name!r} is present in Jira (out-of-band delete?)"
            )
            continue
        needs_update = False
        actual_status_names = {s.name for s in actual.statuses}
        declared_status_names = {s.name for s in want.statuses}
        if not declared_status_names <= actual_status_names:
            needs_update = True
        if actual.description != want.description:
            needs_update = True
        actual_transitions = {t.name: t for t in actual.transitions}
        for spec in _declared_transition_specs(want):
            live = actual_transitions.get(spec.name)
            if live is None:
                needs_update = True
                continue
            if live.to_status != spec.to_status:
                needs_update = True
                continue
            if set(spec.from_statuses) != set(live.from_statuses):
                needs_update = True
                continue
            comparison = _restrict_comparable(
                restrict_rule_parameters(live.conditions),
                spec.restrict_to,
                snapshot,
            )
            if comparison is False:
                needs_update = True
            elif comparison is None and spec.restrict_to:
                r.warnings.append(
                    f"workflow {alias!r} transition {spec.name!r}: restrict holders cannot be "
                    f"verified in the diff (group/user resolution needs live lookups); "
                    f"engine.check reports role drift and the restrict rule is left as-is "
                    f"unless another declared change updates this workflow."
                )
        if needs_update:
            declared_transition_names = {spec.name for spec in _declared_transition_specs(want)}
            r.changes.append(
                UpdateWorkflow(
                    alias=alias,
                    name=want.name,
                    statuses=tuple((s.name, s.category, s.description) for s in want.statuses),
                    initial=want.initial,
                    transitions=_declared_transition_specs(want),
                    version_id=actual.version_id,
                    version_number=actual.version_number,
                    preserve_statuses=tuple(sorted(actual_status_names - declared_status_names)),
                    preserve_transitions=tuple(
                        t.raw for t in actual.transitions if t.name not in declared_transition_names
                    ),
                )
            )
        # Undeclared live transitions are preserved by construction (they ride
        # along in any UpdateWorkflow payload); when no update is needed they
        # simply stay untouched.

    if allow_delete:
        for alias in sorted(state.workflows):
            if alias not in desired.workflows:
                r.changes.append(DeleteWorkflow(alias=alias))
    else:
        _warn_orphans(r, "workflow", state.workflows, desired.workflows)


def _diff_workflow_schemes(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
) -> None:
    """Diff declared workflow schemes against reflection (keyed by name, the
    classic scheme APIs' handle).

    Adoption mirrors workflows: an existing scheme by name is recorded
    (AdoptWorkflowScheme), never rewritten; default/mapping drift is
    reported by engine.check. Once adopted, drift emits UpdateWorkflowScheme
    with the full desired mapping set.
    """
    for alias, want in desired.workflow_schemes.items():
        if alias not in state.workflow_schemes:
            existing = next(
                (s for s in snapshot.workflow_schemes.values() if s.name == want.name and not s.is_draft),
                None,
            )
            if existing is not None:
                r.changes.append(AdoptWorkflowScheme(alias=alias, name=want.name))
                continue
            r.changes.append(
                CreateWorkflowScheme(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    default_workflow=want.default,
                    mappings=tuple(want.mappings),
                )
            )
            continue
        actual = next(
            (s for s in snapshot.workflow_schemes.values() if s.id == state.workflow_schemes[alias].id),
            None,
        )
        if actual is None:
            r.warnings.append(
                f"workflow_scheme {alias!r}: state references id "
                f"{state.workflow_schemes[alias].id!r} but it's not present in Jira "
                f"(out-of-band delete?)"
            )
            continue
        name_change = want.name if actual.name != want.name else None
        desc_change = want.description if actual.description != want.description else None
        default_change = want.default if actual.default_workflow != want.default else None
        mappings_change: tuple[tuple[str, str], ...] | None = None
        if actual.mappings or want.mappings:
            declared_resolved = _resolved_scheme_mappings(want, state)
            actual_by_alias = _mappings_by_alias(actual, state)
            if declared_resolved is None:
                r.warnings.append(
                    f"workflow_scheme {alias!r}: mapping comparison skipped — a declared "
                    f"issuetype or workflow alias is not yet in state (created in this same "
                    f"migration); the next autogenerate run will compare."
                )
            elif declared_resolved != actual_by_alias:
                mappings_change = tuple(want.mappings)
        if (
            name_change is not None
            or desc_change is not None
            or default_change is not None
            or mappings_change is not None
        ):
            r.changes.append(
                UpdateWorkflowScheme(
                    alias=alias,
                    name=name_change,
                    description=desc_change,
                    default_workflow=default_change,
                    mappings=mappings_change,
                )
            )

    if allow_delete:
        for alias in sorted(state.workflow_schemes):
            if alias not in desired.workflow_schemes:
                r.changes.append(DeleteWorkflowScheme(alias=alias))
    else:
        _warn_orphans(r, "workflow_scheme", state.workflow_schemes, desired.workflow_schemes)


def _mappings_by_alias(actual: Any, state: StateFile) -> dict[str, str]:
    """Translate a reflected scheme's issueTypeId→workflow-name mappings into
    declared-alias space (issuetype alias → workflow alias) using the state
    tables. Unknown ids/names are skipped (they compare as drift elsewhere)."""
    id_to_it_alias = {m.id: alias for alias, m in state.issuetypes.items()}
    name_to_wf_alias = {w.name: alias for alias, w in state.workflows.items()}
    out: dict[str, str] = {}
    for it_id, wf_name in (actual.mappings or {}).items():
        it_alias = id_to_it_alias.get(str(it_id))
        wf_alias = name_to_wf_alias.get(str(wf_name))
        if it_alias is not None and wf_alias is not None:
            out[it_alias] = wf_alias
    return out


def _resolved_scheme_mappings(want, state: StateFile) -> dict[str, str] | None:
    """Declared mappings resolved to id-comparable form, or None when a
    referenced issuetype/workflow is not yet in state."""
    out: dict[str, str] = {}
    for it_alias, wf_alias in want.mappings:
        it_mapping = state.issuetypes.get(it_alias)
        wf_mapping = state.workflows.get(wf_alias)
        if it_mapping is None or wf_mapping is None:
            return None
        out[it_alias] = wf_alias
    return out


def _maybe_emit_set_workflow_scheme(
    r: DiffResult,
    *,
    project_alias: str,
    desired_scheme: str,
    state: StateFile,
    actual_scheme_id: str,
    project_in_state: bool,
) -> None:
    """Emit a SetProjectWorkflowScheme when the project's binding differs.

    Existing projects rebind through the async switch endpoint (live=True);
    projects created in this same migration use the sync associate path.
    The desired scheme alias may not be in state yet — the create op runs
    first by phase order, so apply-time alias resolution still works."""
    if not desired_scheme:
        return  # nothing declared: leave the project's binding alone
    state_scheme_id = state.workflow_schemes[desired_scheme].id if desired_scheme in state.workflow_schemes else None
    if actual_scheme_id and state_scheme_id is not None and actual_scheme_id == state_scheme_id:
        return  # in-sync
    r.changes.append(
        SetProjectWorkflowScheme(
            project_alias=project_alias,
            scheme_alias=desired_scheme,
            live=project_in_state,
        )
    )


def _diff_automation_rules(
    r: DiffResult,
    desired: DesiredSnapshot,
    snapshot: Snapshot,
    state: StateFile,
    allow_delete: bool,
    member_params: dict[str, str] | None = None,
) -> None:
    """Diff declared automation rules against reflection (keyed by uuid from
    state; adoption matches an existing rule by exact name on first
    application).

    The payload comparison normalizes both sides (server-generated ids,
    timestamps, tags, and rule metadata dropped) and substitutes declared
    refs into the declared payload — a re-run with no schema change
    compares equal and reports no changes. Scope and enabled state diff
    separately (they have their own endpoints). Undeclared rules are left
    unmanaged, so adopting stint on a project with hand-built rules never
    touches them.
    """
    for alias, want in desired.automation_rules.items():
        desired_scope = _desired_scope_projects(want)
        if alias not in state.automation_rules:
            existing = next(
                (rule for rule in snapshot.automation_rules.values() if rule.name.lower() == want.name.lower()),
                None,
            )
            if existing is not None:
                r.changes.append(AdoptAutomationRule(alias=alias, name=want.name))
                if desired_scope:
                    r.changes.append(SetAutomationRuleScope(alias=alias, scope_projects=desired_scope))
                continue
            r.changes.append(
                CreateAutomationRule(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    payload=want.payload,
                    scope_projects=desired_scope,
                    refs=want.refs,
                    enabled=want.enabled,
                    is_guard=want.is_guard,
                    allowed_writers=want.allowed_writers,
                    actor=want.actor,
                )
            )
            if desired_scope:
                r.changes.append(SetAutomationRuleScope(alias=alias, scope_projects=desired_scope))
            else:
                r.warnings.append(
                    f"automation_rule {alias!r}: no project is bound to the scheme, so the "
                    f"rule is created site-scoped — bind projects to narrow it."
                )
            continue

        mapping = state.automation_rules[alias]
        actual = snapshot.automation_rules.get(mapping.id)
        if actual is None:
            r.warnings.append(
                f"automation_rule {alias!r}: state references uuid {mapping.id!r} but the rule "
                f"is not present in Jira (out-of-band delete?)"
            )
            continue

        comparison = _rule_payload_comparable(want, actual, state, member_params, snapshot)
        # The payload's name is dropped by the normalization (the op's name
        # parameter owns it), so a declared rename is detected here.
        name_drift = mapping.name != want.name
        if comparison is False or name_drift:
            r.changes.append(
                UpdateAutomationRule(
                    alias=alias,
                    name=want.name,
                    description=want.description,
                    payload=want.payload,
                    scope_projects=desired_scope or (),
                    refs=want.refs,
                    is_guard=want.is_guard,
                    allowed_writers=want.allowed_writers,
                    actor=want.actor,
                )
            )
        elif comparison is None:
            r.warnings.append(
                f"automation_rule {alias!r}: payload comparison skipped — a ref or the "
                f"cloudId needed to resolve the declared payload is not resolvable yet "
                f"(created in this same migration?); engine.check reports drift once it is."
            )
        if want.enabled != (actual.state != "DISABLED"):
            r.changes.append(SetAutomationRuleState(alias=alias, enabled=want.enabled))
        if desired_scope and sorted(actual.scope_apis) != _scope_apis(desired_scope, state):
            r.changes.append(SetAutomationRuleScope(alias=alias, scope_projects=desired_scope))

    if allow_delete:
        for alias in sorted(state.automation_rules):
            if alias not in desired.automation_rules:
                r.changes.append(DeleteAutomationRule(alias=alias))
    else:
        _warn_orphans(r, "automation_rule", state.automation_rules, desired.automation_rules)


def _desired_scope_projects(want: DesiredAutomationRule) -> tuple[str, ...]:
    """The declared scope as project keys (empty = site-scoped default).

    The scheme's bound projects come through ``want.scope_projects``; the
    create/scope ops resolve their Jira ids at apply, after project
    creates run. Create carries the scope too — the API refuses a create
    without it.
    """
    return tuple(want.scope_projects)


def _scope_apis(scope_projects: tuple[str, ...], state: StateFile) -> list[str]:
    """Project keys → ruleScopeARIs, from the state file's ids + cloudId.

    Callers only compare scopes when every project id and the cloudId are
    recorded (``_desired_scope_projects`` gates the greenfield path; the
    comparison path checks state coverage before calling).
    """
    cloud_id = state.cloud_id
    apis: list[str] = []
    for key in scope_projects:
        project_id = state.projects[key].id if key in state.projects else ""
        if project_id and cloud_id:
            apis.append(f"ari:cloud:jira:{cloud_id}:project/{project_id}")
    return sorted(apis)


def _rule_payload_comparable(
    want: DesiredAutomationRule,
    actual: Any,
    state: StateFile,
    member_params: dict[str, str] | None,
    snapshot: Snapshot,
) -> bool | None:
    """Three-way payload comparison (True equal / False drift / None unknown).

    None when a ref cannot be resolved offline (a custom-field alias not yet
    in state, an email absent from ``member_params``, a group name — group
    ids need a live lookup): the apply-time resolution is authoritative, the
    diff degrades to a skip warning like the workflow restrict comparison.
    """
    resolved = _diff_resolve_refs(want.refs, state, member_params, snapshot)
    mapping_refs = state.automation_rules[want.alias].refs if want.alias in state.automation_rules else {}
    if resolved is None and mapping_refs:
        # Fall back to the refs a previous apply resolved (recorded in
        # state, like role actors): a re-diff stays idempotent without
        # re-resolving emails/roles.
        resolved = {
            ref_placeholder(kind, ref_name): mapping_refs[ref_name]
            for ref_name, kind, _declared in want.refs
            if mapping_refs.get(ref_name)
        }
    if resolved is None:
        return None
    declared = substitute_refs(json.loads(json.dumps(want.payload)), resolved)
    if unresolved_refs(declared):
        return None
    if normalized_rule_payload(declared, is_guard=want.is_guard) != normalized_rule_payload(
        actual.payload, is_guard=want.is_guard
    ):
        return False
    return True


def _diff_resolve_refs(
    refs: tuple[tuple[str, str, str], ...],
    state: StateFile,
    member_params: dict[str, str] | None,
    snapshot: Snapshot,
) -> dict[str, str] | None:
    """Resolve declared refs to substitution values for the diff, or None.

    Fields resolve through state (aliases -> customfield ids), users through
    ``member_params`` (pre-resolved emails; raw accountIds pass through),
    roles through the reflected global role map. Groups are not resolvable
    offline (ids come from a live lookup) and force the None verdict.
    """
    out: dict[str, str] = {}
    for ref_name, kind, declared in refs:
        placeholder = ref_placeholder(kind, ref_name)
        if kind == "field":
            field_mapping = state.custom_fields.get(declared)
            if field_mapping is None:
                return None
            out[placeholder] = field_mapping.id
        elif kind == "system":
            out[placeholder] = declared
        elif kind == "user":
            resolved = (member_params or {}).get(declared)
            if resolved is None and "@" not in declared:
                resolved = declared
            if resolved is None:
                return None
            out[placeholder] = resolved
        elif kind == "role":
            resolved = (snapshot.project_roles or {}).get(declared)
            if resolved is None:
                return None
            out[placeholder] = resolved
        else:  # group: ids need a live lookup
            return None
    return out


def _maybe_emit_set_scheme(
    r: DiffResult,
    *,
    project_alias: str,
    desired_scheme: str | None,
    state_table: dict,
    actual_id: str | None,
    change_cls: type,
) -> None:
    """Emit a SetProject*Scheme change when the existing project's binding
    differs from what the schema declares.

    Skipped only when the binding is already in sync. When the desired
    scheme's alias is not yet in state (it's being created in the same
    migration, by a lower-phase op), the RE-BIND still must fire: this is the
    brownfield path (a Project that already exists in Jira gains a new
    scheme), and here no create-side change will ever emit the binding that
    a brand-new project's create path emits. Phase ordering guarantees the
    create op runs (and records its id in state) before the binding op
    resolves it at apply time."""
    if desired_scheme is None:
        return
    desired_mapping = state_table.get(desired_scheme)
    if actual_id is not None and desired_mapping is not None and actual_id == desired_mapping.id:
        return  # in-sync
    r.changes.append(change_cls(project_alias=project_alias, scheme_alias=desired_scheme))


def _find_project_by_id(snapshot: Snapshot, project_id: str):
    for proj in snapshot.projects.values():
        if proj.id == project_id:
            return proj
    return None


def _itss_mappings_changed(want, actual, state: StateFile) -> dict[str, str] | None:
    """Resolve desired mappings (alias→alias) to id-comparable form and compare
    with the snapshot's mappings tuple. Return desired mappings if different,
    None if identical."""
    want_resolved: dict[str, str] = {}
    for it_alias, ss_alias in want.mappings.items():
        ss_mapping = state.screen_schemes.get(ss_alias)
        if ss_mapping is None:
            # Not yet in state (probably being created in same migration).
            # Skip mapping comparison; we'll re-emit on next autogen run.
            return None
        if it_alias == "default":
            it_id = "default"
        else:
            it_mapping = state.issuetypes.get(it_alias)
            if it_mapping is None:
                return None
            it_id = it_mapping.id
        want_resolved[it_id] = ss_mapping.id
    actual_resolved = {m.issuetype_id: m.screen_scheme_id for m in actual.mappings}
    if want_resolved == actual_resolved:
        return None
    return dict(want.mappings)


def _fcs_mappings_changed(want, actual, state: StateFile) -> dict[str, str] | None:
    """Same as _itss_mappings_changed for field configuration schemes."""
    # v2 Field Schemes have no work-type → configuration mapping table: the
    # dialect reflects an empty tuple, and per-work-type behavior lives in
    # the scheme's inline field parameters. Treat that as "cannot compare"
    # rather than emitting a rebind on every autogenerate run.
    if not actual.mappings:
        return None
    want_resolved: dict[str, str] = {}
    for it_alias, fc_alias in want.mappings.items():
        fc_mapping = state.field_configurations.get(fc_alias)
        if fc_mapping is None:
            return None
        if it_alias == "default":
            it_id = "default"
        else:
            it_mapping = state.issuetypes.get(it_alias)
            if it_mapping is None:
                return None
            it_id = it_mapping.id
        want_resolved[it_id] = fc_mapping.id
    actual_resolved = {m.issuetype_id: m.field_configuration_id for m in actual.mappings}
    if want_resolved == actual_resolved:
        return None
    return dict(want.mappings)


# ── Helpers ──────────────────────────────────────────────────────────
def _warn_orphans(
    r: DiffResult,
    type_name: str,
    state_table: dict,
    desired_table: dict,
) -> None:
    for alias in sorted(state_table):
        if alias not in desired_table:
            r.warnings.append(
                f"{type_name} {alias!r} is in state but not in schema. "
                f"Re-run with --allow-delete to drop, or restore it to the schema."
            )


def _missing_screen_fields(
    want: Any,
    actual: Any,
    state: StateFile,
) -> list[str]:
    """L21 helper: declared screen fields (aliases) whose resolved Jira field
    id appears on no tab of the reflected screen. Resolution goes through
    state (custom-field alias → id); anything else is normalized to Jira's
    system-field id the same way ``op.add_screen_tab_field`` writes it."""
    actual_fields: set[str] = set()
    for tab in actual.tabs:
        actual_fields.update(tab.fields)
    missing: list[str] = []
    for tab in want.tabs:
        for ref in tab.field_refs:
            mapping = state.custom_fields.get(ref)
            field_id = mapping.id if mapping is not None else jira_system_field_id(ref)
            if field_id not in actual_fields:
                missing.append(f"{ref!r} ({field_id!r})")
    return missing


def _field_configuration_item_drift(
    want: Any,
    actual: Any,
    state: StateFile,
) -> str | None:
    """L21 helper for existing field configurations: describe declared-item
    drift that reflection can see but the diff cannot act on (op-level item
    updates for existing FCs need an alias→field_id round-trip). Non-state
    references normalize to Jira's system-field id like the ops write them."""
    problems: list[str] = []
    for item in want.items:
        mapping = state.custom_fields.get(item.field_alias)
        field_id = mapping.id if mapping is not None else jira_system_field_id(item.field_alias)
        existing = actual.items.get(field_id)
        if existing is None:
            problems.append(f"declared item {item.field_alias!r} ({field_id!r}) is not in the Jira configuration")
        elif existing.required != item.required or existing.hidden != item.hidden:
            problems.append(
                f"item {item.field_alias!r} (required={existing.required}, hidden={existing.hidden}) "
                f"drifts from the schema (required={item.required}, hidden={item.hidden})"
            )
    return "; ".join(problems) or None
