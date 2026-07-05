"""TMP reconcile: (desired, reflected, state) -> a small op plan, then apply.

Mirrors the shape of ``stint.autogen.diff``/``stint.autogen.emit`` (a pure
planning step producing typed Change records, then something that executes
them) without reusing any of it: CMP's ``Change`` vocabulary and phase-sort
are built for granular, incremental deltas rendered into migration source
files. TMP writes are full-replacement, so its plan is coarser (a handful of
Change kinds, no separate "render to Python source" step) and executes
directly against ``TmpDialect``/``TmpState`` rather than through
``stint.migrations``. See ``ops.py``'s module docstring for the full
rationale, and ``tmp_dialect_design.md``'s "Reconcile / diff" section for the
design record this implements.
"""

from __future__ import annotations

from dataclasses import dataclass

from stint.dialects.jira.tmp.desired import TmpDesired, TmpDesiredWorkType
from stint.dialects.jira.tmp.models import TmpLayout, TmpSnapshot
from stint.dialects.jira.tmp.ops import (
    TmpApplyContext,
    tmp_delete_field,
    tmp_delete_worktype,
    tmp_set_layout,
    tmp_upsert_field,
    tmp_upsert_worktype,
)
from stint.dialects.jira.tmp.state import TmpState
from stint.state.snapshot import CustomFieldSnapshot


@dataclass(frozen=True)
class CreateField:
    alias: str


@dataclass(frozen=True)
class UpdateField:
    alias: str


@dataclass(frozen=True)
class DeleteField:
    alias: str


@dataclass(frozen=True)
class CreateWorkType:
    alias: str


@dataclass(frozen=True)
class DeleteWorkType:
    alias: str


@dataclass(frozen=True)
class SetLayout:
    alias: str  # work-type alias


TmpChange = CreateField | UpdateField | DeleteField | CreateWorkType | DeleteWorkType | SetLayout

# Apply order: fields before the work types/layouts that reference them,
# worktypes before their layouts, deletes last (mirrors autogen/emit.py's
# phase-sort convention, adapted to TMP's smaller op set).
_APPLY_PHASE: dict[type, int] = {
    CreateField: 0,
    UpdateField: 0,
    CreateWorkType: 1,
    SetLayout: 2,
    DeleteWorkType: 3,
    DeleteField: 4,
}


def sort_tmp_changes(changes: list[TmpChange]) -> list[TmpChange]:
    return sorted(changes, key=lambda c: _APPLY_PHASE[type(c)])


def _field_drifted(current: CustomFieldSnapshot, desired_options: tuple[str, ...], desired_name: str) -> bool:
    if current.name != desired_name:
        return True
    return set(current.options) != set(desired_options)


def _layout_drifted(current: TmpLayout, desired: TmpDesiredWorkType, desired_field_ids: set[str]) -> bool:
    if current.owner.name != desired.name or current.owner.description != desired.description:
        return True
    current_custom_ids = {i.field_id for i in current.items if i.custom}
    return current_custom_ids != desired_field_ids


def plan_tmp(
    desired: TmpDesired,
    snapshot: TmpSnapshot,
    state: TmpState,
    *,
    allow_delete: bool = False,
) -> list[TmpChange]:
    """Pure: no I/O. Compares desired schema against reflected state + the
    alias->id map, returns the ops needed to reconcile them.

    Deletions are opt-in (``allow_delete``, mirroring ``autogen.diff``'s same
    safety default) since TMP writes have no undo.
    """
    changes: list[TmpChange] = []

    for alias, df in desired.fields.items():
        if alias not in state.fields:
            changes.append(CreateField(alias))
            continue
        current = snapshot.snapshot.custom_fields.get(state.fields[alias])
        if current is None or _field_drifted(current, df.options, df.name):
            changes.append(UpdateField(alias))

    if allow_delete:
        for alias in state.fields:
            if alias not in desired.fields:
                changes.append(DeleteField(alias))

    for alias, dw in desired.worktypes.items():
        if alias not in state.worktypes:
            changes.append(CreateWorkType(alias))
            # Can't diff a layout for a work type that doesn't exist yet.
            changes.append(SetLayout(alias))
            continue

        # If any declared field for this worktype is itself being created in
        # this same pass, its id isn't in `state` yet -- can't diff precisely,
        # so resync unconditionally rather than risk under-detecting drift.
        if any(a not in state.fields for a in dw.field_aliases):
            changes.append(SetLayout(alias))
            continue

        worktype_id = state.worktypes[alias]
        current_layout = snapshot.layouts.get(worktype_id)
        desired_field_ids = {state.fields[a] for a in dw.field_aliases}
        if current_layout is None or _layout_drifted(current_layout, dw, desired_field_ids):
            changes.append(SetLayout(alias))

    if allow_delete:
        for alias in state.worktypes:
            if alias not in desired.worktypes:
                changes.append(DeleteWorkType(alias))

    return sort_tmp_changes(changes)


async def apply_tmp_plan(
    ctx: TmpApplyContext, changes: list[TmpChange], desired: TmpDesired, snapshot: TmpSnapshot
) -> None:
    """Execute a plan produced by ``plan_tmp``, in a safe (already-sorted) order."""
    for change in changes:
        if isinstance(change, CreateField):
            await tmp_upsert_field(ctx, desired.fields[change.alias], current_field_id=None)
        elif isinstance(change, UpdateField):
            current_id = ctx.state.fields[change.alias]
            current = snapshot.snapshot.custom_fields.get(current_id)
            await tmp_upsert_field(
                ctx,
                desired.fields[change.alias],
                current_field_id=current_id,
                current_options=current.options if current else None,
            )
        elif isinstance(change, DeleteField):
            await tmp_delete_field(ctx, change.alias)
        elif isinstance(change, CreateWorkType):
            await tmp_upsert_worktype(ctx, desired.worktypes[change.alias])
        elif isinstance(change, DeleteWorkType):
            await tmp_delete_worktype(ctx, change.alias)
        elif isinstance(change, SetLayout):
            worktype_id = ctx.state.worktypes[change.alias]
            current_layout = snapshot.layouts.get(worktype_id)
            if current_layout is None:
                current_layout = await ctx.dialect.read_layout(
                    project_id=ctx.project.project_id, issuetype_id=int(worktype_id)
                )
            await tmp_set_layout(ctx, change.alias, desired.worktypes[change.alias], current_layout)
