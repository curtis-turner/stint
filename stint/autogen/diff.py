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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stint.autogen.desired import (
    DesiredSnapshot,
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
) -> DiffResult:
    """Return ordered Change list + warnings. Phase order applied by emit.

    `allow_delete` is required for any destructive op (delete_*, remove_option).
    Destructive ops are silently dropped (with a warning) when False.
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
    _diff_security_schemes(r, desired, snapshot, state, allow_delete)
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
            actual_params = {m.parameter: m.type for m in found.members if m.parameter}
            for member in level.members:
                if member.value in recorded:
                    continue
                if actual_params.get(member.value) is not None:
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
    state (custom-field alias → id); anything else is treated as a Jira
    field id directly (system fields are addressed by name)."""
    actual_fields: set[str] = set()
    for tab in actual.tabs:
        actual_fields.update(tab.fields)
    missing: list[str] = []
    for tab in want.tabs:
        for ref in tab.field_refs:
            mapping = state.custom_fields.get(ref)
            field_id = mapping.id if mapping is not None else ref
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
    updates for existing FCs need an alias→field_id round-trip)."""
    problems: list[str] = []
    for item in want.items:
        mapping = state.custom_fields.get(item.field_alias)
        field_id = mapping.id if mapping is not None else item.field_alias
        existing = actual.items.get(field_id)
        if existing is None:
            problems.append(f"declared item {item.field_alias!r} ({field_id!r}) is not in the Jira configuration")
        elif existing.required != item.required or existing.hidden != item.hidden:
            problems.append(
                f"item {item.field_alias!r} (required={existing.required}, hidden={existing.hidden}) "
                f"drifts from the schema (required={item.required}, hidden={item.hidden})"
            )
    return "; ".join(problems) or None
