"""TMP op set: full-replacement operations against a TmpDialect + TmpState.

Deliberately not ``stint.migrations.op``: CMP's op vocabulary is built for
incremental deltas (``AddCustomFieldOption``, granular scheme-mapping
changes) rendered into migration source files via ``autogen/emit.py``. TMP's
wire behavior is full-replacement (``editCustomField`` replaces the whole
options array, ``write_layout`` PUTs the entire item list), which doesn't fit
that per-Change-type model. These functions operate directly against a
``TmpDialect`` + ``TmpState`` -- not ``stint.migrations.context.
MigrationContext`` -- so this stays fully inside ``stint/dialects/jira/tmp/``
with zero coupling to core migration machinery. Wiring these into real
migrations/CLI (which needs the ``Engine.dialect`` typing question resolved)
is deferred to a later phase.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from stint.dialects.jira.tmp.desired import TmpDesired, TmpDesiredField, TmpDesiredWorkType
from stint.dialects.jira.tmp.dialect import TmpDialect
from stint.dialects.jira.tmp.models import (
    TmpFieldOption,
    TmpLayout,
    TmpLayoutItem,
    TmpLayoutOwner,
    TmpProjectContext,
    TmpSnapshot,
)
from stint.dialects.jira.tmp.state import TmpState


@dataclass
class TmpApplyContext:
    """Everything an op needs: the dialect, the resolved project, and state.

    ``on_state_changed`` is an optional sink the CLI passes in to checkpoint
    the state file after each successful state mutation -- the same
    fix-forward pattern ``stint.migrations.context.MigrationContext.persist``
    uses. Ops call ``ctx.persist()`` once their state mutation is committed,
    so a mid-plan failure leaves everything that already succeeded recorded
    on disk: a retry sees them, and ``--allow-delete`` can target anything
    orphaned from a previous run. ``None`` (the unit-test default) means
    "don't checkpoint" -- tests can observe final state without I/O.
    """

    dialect: TmpDialect
    project: TmpProjectContext
    state: TmpState
    on_state_changed: Callable[[], None] | None = None

    def persist(self) -> None:
        """Invoke the checkpoint sink if one was provided. No-op otherwise.

        Each op calls this once its state mutation has been committed, so a
        mid-plan failure leaves everything that already succeeded recorded
        on disk -- same fix-forward contract as
        ``MigrationContext.persist``, just routed through a callable so the
        TMP CLI can keep the ``tmp_projects`` mapping in one place.
        """
        if self.on_state_changed is not None:
            self.on_state_changed()


async def tmp_upsert_field(
    ctx: TmpApplyContext,
    desired: TmpDesiredField,
    *,
    current_field_id: str | None,
    current_options: dict[str, str] | None = None,
) -> None:
    """Create the field if unseen; otherwise send the full-replacement edit.

    ``current_options`` (value -> existing optionId, from the reflected
    ``CustomFieldSnapshot``) lets already-existing option values keep their
    id instead of being recreated -- only genuinely new values get
    ``option_id=None`` (and so a fresh id from the API).
    """
    existing = current_options or {}
    options = [TmpFieldOption(value=v, option_id=existing.get(v)) for v in desired.options]
    if current_field_id is None:
        result = await ctx.dialect.create_field(
            cloud_id=ctx.project.cloud_id,
            project_id=ctx.project.project_id,
            type_key=desired.type_key,
            name=desired.name,
            description=desired.description,
            options=options,
        )
    else:
        result = await ctx.dialect.edit_field(
            cloud_id=ctx.project.cloud_id,
            project_id=ctx.project.project_id,
            field_id=current_field_id,
            name=desired.name,
            description=desired.description,
            options=options,
        )
    ctx.state.fields[desired.alias] = result.field_id
    ctx.persist()


async def tmp_delete_field(ctx: TmpApplyContext, alias: str) -> None:
    field_id = ctx.state.fields.get(alias)
    if field_id is None:
        return
    await ctx.dialect.delete_field(cloud_id=ctx.project.cloud_id, project_id=ctx.project.project_id, field_id=field_id)
    del ctx.state.fields[alias]
    ctx.persist()


async def tmp_upsert_worktype(ctx: TmpApplyContext, desired: TmpDesiredWorkType) -> None:
    """Create the work type if unseen.

    No update branch: there is no work-type update endpoint (see
    ``TmpDialect.create_worktype``'s docstring) -- renaming happens through
    ``tmp_set_layout``'s owner data instead.
    """
    if desired.alias in ctx.state.worktypes:
        return
    result = await ctx.dialect.create_worktype(
        project_id=ctx.project.project_id,
        project_uuid=ctx.project.project_uuid,
        name=desired.name,
        description=desired.description,
    )
    ctx.state.worktypes[desired.alias] = result.id
    ctx.persist()


async def tmp_delete_worktype(ctx: TmpApplyContext, alias: str) -> None:
    worktype_id = ctx.state.worktypes.get(alias)
    if worktype_id is None:
        return
    await ctx.dialect.delete_worktype(project_id=ctx.project.project_id, worktype_id=worktype_id)
    del ctx.state.worktypes[alias]
    ctx.state.layout_ids.pop(alias, None)
    ctx.persist()


async def tmp_set_layout(
    ctx: TmpApplyContext,
    worktype_alias: str,
    desired: TmpDesiredWorkType,
    current: TmpLayout,
    *,
    snapshot: TmpSnapshot | None = None,
    desired_all: TmpDesired | None = None,
) -> None:
    """Full-replacement layout write: resync the owner's name/description,
    prune custom fields no longer declared for this work type, and add
    items for newly-declared custom fields that don't already appear on
    ``current.items``.

    Adding items for new fields is the critical part for brand-new work
    types: a freshly-created work type's layout has zero custom items
    (confirmed live), and ``write_layout`` is full-replacement -- so
    without the add branch, declaring a new ``IssueType`` together with its
    fields in the same schema produces a work type whose edit screen shows
    none of the declared fields. ``apply`` would silently report full
    success.

    Where new items come from, in order:

    1. **Other layouts in the snapshot** -- the cheap, always-tried path.
       For every declared custom field, look for any existing work type's
       layout item in the snapshot whose ``field_id`` matches. Copying
       that item preserves every Jira-controlled field (operations,
       provider, externalUuid, section/position relative to its source
       work type, etc.) exactly as Jira expects.

    2. **A fresh ``read_layout`` of a sibling work type** -- when the
       snapshot doesn't carry the field (the field was created earlier in
       this same apply run, or the snapshot was reflected before a recent
       layout edit). Try any other work type the apply run knows about;
       the first one with the field wins.

    3. **Minimal synthesis** -- always-available fallback. We know the
       field id, name, and type from ``desired_all.fields`` (or the
       derived state). Synthesizing an item with just those plus
       ``custom=True``, ``section="primary"``, a fresh UUID, and empty
       operations/provider is enough for ``write_layout`` to round-trip
       successfully; Jira fills in the rest on the response.

    ``snapshot`` and ``desired_all`` are optional; when omitted, step 1
    uses only ``current.items`` (so a unit test that builds a ``TmpLayout``
    by hand still exercises the kept-items path), and step 2 is skipped
    entirely. Step 3 (synthesis) is always available regardless -- a
    schema-only call with no snapshot at all still produces a working
    layout.

    Reordering is still a deliberately deferred gap (no live capture to
    validate a reorder algorithm against, and getting this wrong on a
    full-replacement write would be a real, visible regression -- see
    module docstring). New items are appended after ``kept_items`` in the
    order they appear in ``desired.field_aliases``.
    """
    desired_field_ids = {ctx.state.fields[a] for a in desired.field_aliases if a in ctx.state.fields}
    kept_items = tuple(item for item in current.items if not item.custom or item.field_id in desired_field_ids)
    kept_field_ids = {item.field_id for item in kept_items if item.custom}

    next_position = max((item.position for item in kept_items), default=0) + 100
    new_items: list[TmpLayoutItem] = list(kept_items)
    for alias in desired.field_aliases:
        field_id = ctx.state.fields.get(alias)
        if field_id is None or field_id in kept_field_ids:
            continue
        template = _find_template_in_snapshot(field_id, snapshot)
        if template is None:
            template = await _find_template_remote(ctx, field_id, skip_worktype_alias=worktype_alias)
        df = desired_all.fields.get(alias) if desired_all is not None else None
        if template is not None:
            new_items.append(template)
        else:
            new_items.append(_synthesize_template_item(field_id, alias, df, next_position))
        kept_field_ids.add(field_id)
        next_position += 100

    new_layout = TmpLayout(
        layout_id=current.layout_id,
        owner=TmpLayoutOwner(
            id=current.owner.id,
            name=desired.name,
            description=desired.description,
            avatar_id=current.owner.avatar_id,
            icon_url=current.owner.icon_url,
        ),
        items=tuple(new_items),
    )
    written = await ctx.dialect.write_layout(
        project_id=ctx.project.project_id,
        issuetype_id=int(ctx.state.worktypes[worktype_alias]),
        layout=new_layout,
    )
    ctx.state.layout_ids[worktype_alias] = written.layout_id
    ctx.persist()


def _find_template_in_snapshot(field_id: str, snapshot: TmpSnapshot | None) -> TmpLayoutItem | None:
    """Step 1 of the template hunt: scan the snapshot for any layout
    containing ``field_id``. Pure dict scan, no I/O."""
    if snapshot is None:
        return None
    for layout in snapshot.layouts.values():
        for item in layout.items:
            if item.field_id == field_id:
                return item
    return None


async def _find_template_remote(
    ctx: TmpApplyContext,
    field_id: str,
    *,
    skip_worktype_alias: str,
) -> TmpLayoutItem | None:
    """Step 2 of the template hunt: fresh-read any other work type the
    apply run knows about, looking for ``field_id``.

    Used when the snapshot doesn't carry the field (the field was created
    earlier in this same apply run, or the snapshot was reflected before
    a recent layout edit). Iterates ``state.worktypes`` -- which already
    has the new work type's id after ``tmp_upsert_worktype`` ran -- and
    reads the first one (other than the one we're updating) that has it.
    Most cases stop at the first sibling.
    """
    for other_alias, other_id in ctx.state.worktypes.items():
        if other_alias == skip_worktype_alias:
            continue
        try:
            layout = await ctx.dialect.read_layout(project_id=ctx.project.project_id, issuetype_id=int(other_id))
        except Exception:
            continue
        for item in layout.items:
            if item.field_id == field_id:
                return item
    return None


def _synthesize_template_item(
    field_id: str,
    alias: str,
    desired_field: TmpDesiredField | None,
    position: int,
) -> TmpLayoutItem:
    """Minimal ``TmpLayoutItem`` for a field with no source template.

    Used as the always-available fallback in ``tmp_set_layout``. Wire
    shape matches what a freshly-created field's Jira-side layout entry
    carries: ``custom=True``, ``global_=False``, ``section="primary"``,
    ``required=False``, a fresh ``externalUuid``, empty
    operations/provider maps. Jira accepts this on PUT and rehydrates the
    response with the canonical item shape.
    """
    name = desired_field.name if desired_field is not None else alias
    type_key = desired_field.type_key if desired_field is not None else ""
    description = desired_field.description if desired_field is not None else ""
    return TmpLayoutItem(
        field_id=field_id,
        key=field_id,
        name=name,
        type_key=type_key,
        custom=True,
        global_=False,
        required=False,
        section="primary",
        position=position,
        external_uuid=str(uuid.uuid4()),
        description=description,
        operations={},
        provider={},
    )
