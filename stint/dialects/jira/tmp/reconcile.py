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

from stint.dialects.jira.tmp.desired import TmpDesired, TmpDesiredField, TmpDesiredWorkType
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
from stint.exceptions import ConfigurationError
from stint.state.snapshot import CustomFieldSnapshot


@dataclass(frozen=True)
class CreateField:
    alias: str


@dataclass(frozen=True)
class AdoptField:
    """Bind a desired alias to an existing, untracked project-scoped field.

    Emitted instead of ``CreateField`` when reflect sees a field whose name
    and type match a declared field that has no alias->id mapping in state
    yet. This is the recovery path for a field that exists in Jira but not
    in state -- an orphan left by a pre-fix partial apply, or a field created
    by hand. Adopting it makes it visible to the normal delete-reconciliation
    path (``--allow-delete``) and prevents a duplicate create.
    """

    alias: str
    field_id: str


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


TmpChange = CreateField | AdoptField | UpdateField | DeleteField | CreateWorkType | DeleteWorkType | SetLayout


@dataclass(frozen=True)
class UntrackedField:
    """A project-scoped field reflect saw but neither state nor schema owns.

    Reported by ``find_untracked_fields`` so ``stint apply`` can tell the user
    it exists instead of leaving it permanently invisible to the schema-driven
    delete path.
    """

    field_id: str
    name: str
    type_id: str


class TmpAmbiguousFieldError(ConfigurationError):
    """More than one untracked field matches a declared field's name and type.

    Adopting either one would be a guess, and creating a third field would
    duplicate existing data. The user has to disambiguate first (rename one
    of the Jira fields, or delete one by hand).
    """


# Apply order: fields before the work types/layouts that reference them,
# worktypes before their layouts, deletes last (mirrors autogen/emit.py's
# phase-sort convention, adapted to TMP's smaller op set). AdoptField shares
# CreateField's phase and, when emitted, precedes that alias's UpdateField so
# the update sees the newly-adopted id in state.
_APPLY_PHASE: dict[type, int] = {
    CreateField: 0,
    AdoptField: 0,
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


def _untracked_fields_by_name(snapshot: TmpSnapshot, state: TmpState) -> dict[str, list[CustomFieldSnapshot]]:
    """Index reflected project-scoped fields that state does not own by name.

    ``Snapshot.custom_fields`` only contains ``scope == "PROJECT"`` fields for
    the TMP reflect path, so this cannot pick up tenant-wide fields. Fields
    already mapped to an alias are excluded: they are tracked, not adoptable.
    """
    tracked_ids = set(state.fields.values())
    by_name: dict[str, list[CustomFieldSnapshot]] = {}
    for field_id, field in snapshot.snapshot.custom_fields.items():
        if field_id in tracked_ids:
            continue
        by_name.setdefault(field.name, []).append(field)
    return by_name


def _adoption_candidate(
    desired: TmpDesiredField,
    by_name: dict[str, list[CustomFieldSnapshot]],
    claimed_ids: set[str],
) -> CustomFieldSnapshot | None:
    """Return the one untracked field matching ``desired``'s name and type.

    Returns ``None`` when nothing matches (caller creates a new field).
    Raises when more than one field matches: silently picking one would
    bind the schema to an arbitrary Jira object, and creating a new field
    would leave duplicates behind.
    """
    candidates = [f for f in by_name.get(desired.name, []) if f.id not in claimed_ids]
    matching = [f for f in candidates if f.type_id == desired.type_key]
    if len(matching) > 1:
        ids = ", ".join(sorted(f.id for f in matching))
        raise TmpAmbiguousFieldError(
            f"custom field {desired.name!r} (alias {desired.alias!r}) matches "
            f"{len(matching)} untracked project fields: {ids}. Rename or delete "
            f"the extra field(s) in Jira, then re-run apply."
        )
    return matching[0] if matching else None


def find_untracked_fields(desired: TmpDesired, snapshot: TmpSnapshot, state: TmpState) -> tuple[UntrackedField, ...]:
    """Project-scoped fields reflect sees that neither state nor schema owns.

    These are the fields that ``plan_tmp`` cannot delete, because it only
    diffs state against desired: they have no alias->id mapping, so
    ``--allow-delete`` cannot see them either. Surfacing them is the
    recovery path the draft calls out -- and adoption (see ``AdoptField``)
    is how a user can bring one back under management without hand-editing
    state or querying Jira's internal GraphQL API.

    Fields that ``plan_tmp`` would adopt are not reported: they are about to
    become tracked.
    """
    by_name = _untracked_fields_by_name(snapshot, state)
    claimed_ids: set[str] = set()
    for alias, df in desired.fields.items():
        if alias in state.fields:
            continue
        try:
            candidate = _adoption_candidate(df, by_name, claimed_ids)
        except TmpAmbiguousFieldError:
            # plan_tmp raises on this before apply writes anything; report the
            # ambiguous fields as untracked so the plan output is still useful.
            continue
        if candidate is not None:
            claimed_ids.add(candidate.id)

    tracked_ids = set(state.fields.values())
    leftovers = (
        UntrackedField(field_id=field.id, name=field.name, type_id=field.type_id)
        for field in snapshot.snapshot.custom_fields.values()
        if field.id not in tracked_ids and field.id not in claimed_ids
    )
    return tuple(sorted(leftovers, key=lambda f: (f.name, f.field_id)))


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

    A desired field with no alias->id mapping is matched against reflected
    project-scoped fields by name *and* type first: a single match is adopted
    (``AdoptField``) instead of re-created. That keeps retries idempotent when
    state was lost or a pre-per-op-persist run left an orphan behind, and it
    brings the field under management so a later ``--allow-delete`` can reach
    it. No match creates a new field; more than one match raises
    ``TmpAmbiguousFieldError`` rather than guessing.

    Deletions are opt-in (``allow_delete``, mirroring ``autogen.diff``'s same
    safety default) since TMP writes have no undo.
    """
    changes: list[TmpChange] = []
    untracked_by_name = _untracked_fields_by_name(snapshot, state)
    claimed_ids: set[str] = set()

    for alias, df in desired.fields.items():
        if alias not in state.fields:
            candidate = _adoption_candidate(df, untracked_by_name, claimed_ids)
            if candidate is None:
                changes.append(CreateField(alias))
                continue
            claimed_ids.add(candidate.id)
            changes.append(AdoptField(alias, candidate.id))
            # Adopted fields can still have drifted (renamed, options changed
            # since the orphan was created); let the normal update path fix it.
            if _field_drifted(candidate, df.options, df.name):
                changes.append(UpdateField(alias))
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
        elif isinstance(change, AdoptField):
            # No wire call: the field already exists. Record the mapping and
            # checkpoint immediately, exactly like a create, so the adopted
            # field is on disk before the next op can fail.
            ctx.state.fields[change.alias] = change.field_id
            ctx.persist()
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
            await tmp_set_layout(
                ctx,
                change.alias,
                desired.worktypes[change.alias],
                current_layout,
                snapshot=snapshot,
                desired_all=desired,
            )
