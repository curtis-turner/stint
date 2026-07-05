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

from dataclasses import dataclass

from stint.dialects.jira.tmp.desired import TmpDesiredField, TmpDesiredWorkType
from stint.dialects.jira.tmp.dialect import TmpDialect
from stint.dialects.jira.tmp.models import TmpFieldOption, TmpLayout, TmpLayoutOwner, TmpProjectContext
from stint.dialects.jira.tmp.state import TmpState


@dataclass
class TmpApplyContext:
    """Everything an op needs: the dialect, the resolved project, and state."""

    dialect: TmpDialect
    project: TmpProjectContext
    state: TmpState


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


async def tmp_delete_field(ctx: TmpApplyContext, alias: str) -> None:
    field_id = ctx.state.fields.get(alias)
    if field_id is None:
        return
    await ctx.dialect.delete_field(cloud_id=ctx.project.cloud_id, project_id=ctx.project.project_id, field_id=field_id)
    del ctx.state.fields[alias]


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


async def tmp_delete_worktype(ctx: TmpApplyContext, alias: str) -> None:
    worktype_id = ctx.state.worktypes.get(alias)
    if worktype_id is None:
        return
    await ctx.dialect.delete_worktype(project_id=ctx.project.project_id, worktype_id=worktype_id)
    del ctx.state.worktypes[alias]
    ctx.state.layout_ids.pop(alias, None)


async def tmp_set_layout(
    ctx: TmpApplyContext,
    worktype_alias: str,
    desired: TmpDesiredWorkType,
    current: TmpLayout,
) -> None:
    """Full-replacement layout write: resync the owner's name/description and
    prune custom fields no longer declared for this work type.

    Does NOT reorder fields to match declared attribute order, and does NOT
    add items for newly-declared fields -- ``createCustomFieldInProjectAndAdd
    ToAllIssueTypes`` already associates a new field to every work type at
    creation time, so by the time this runs (after field creates/edits in
    apply order) a newly-declared field is already on ``current.items``.
    This op's job is purely to remove fields no longer wanted here and keep
    the owner's rename/description in sync. Reordering is a documented,
    deliberately deferred gap (no live capture to validate a reorder
    algorithm against, and getting this wrong on a full-replacement write
    would be a real, visible regression for users -- see module docstring).
    """
    desired_field_ids = {ctx.state.fields[a] for a in desired.field_aliases if a in ctx.state.fields}
    kept_items = tuple(item for item in current.items if not item.custom or item.field_id in desired_field_ids)
    new_layout = TmpLayout(
        layout_id=current.layout_id,
        owner=TmpLayoutOwner(
            id=current.owner.id,
            name=desired.name,
            description=desired.description,
            avatar_id=current.owner.avatar_id,
            icon_url=current.owner.icon_url,
        ),
        items=kept_items,
    )
    written = await ctx.dialect.write_layout(
        project_id=ctx.project.project_id,
        issuetype_id=int(ctx.state.worktypes[worktype_alias]),
        layout=new_layout,
    )
    ctx.state.layout_ids[worktype_alias] = written.layout_id
