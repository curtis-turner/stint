"""Operations a migration body can perform.

Each op:
  - Reads the active MigrationContext (engine + state)
  - Calls the dialect to perform the change against Jira
  - Updates the state file with newly-assigned Jira IDs
  - Returns nothing (or the new ID, for ops that emit one)

All ops are alias-keyed. The migration body never references a Jira ID
directly; the op resolves parent aliases against state. This is what makes
migrations portable across environments (dev's customfield_10042 may be
prod's customfield_10501; the alias is the same).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import Any

from stint.exceptions import ConfigurationError
from stint.fields import _FieldType
from stint.migrations.context import get_context
from stint.migrations.exceptions import UnsupportedDowngradeError
from stint.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    ScreenMapping,
    SecuritySchemeMapping,
    SimpleMapping,
)


# ── Custom fields ────────────────────────────────────────────────────
async def create_custom_field(
    *,
    alias: str,
    name: str,
    type: type[_FieldType],
    description: str = "",
    options: Iterable[str] | None = None,
    searcher_key: str | None = None,
) -> str:
    """Create a Jira custom field and register its alias→ID mapping.

    For select-style fields, also creates each option and records the
    option-name → option-ID mapping in the state file.

    Returns the new Jira field id (e.g. "customfield_10042"). Idempotent:
    re-running with an alias already present in state returns the existing id
    without hitting Jira (also adds any missing options).
    """
    ctx = get_context()
    _require_alias(alias, "create_custom_field")
    existing = ctx.state.custom_fields.get(alias)
    if existing is not None:
        # Idempotent path: fill in any missing options, return.
        for opt_value in options or []:
            if opt_value not in existing.options:
                opt_id = await ctx.engine.dialect.add_custom_field_option(
                    existing.id,
                    opt_value,
                )
                existing.options[opt_value] = opt_id
                ctx.persist()
        return existing.id

    field_id = await ctx.engine.dialect.create_custom_field(
        name=name,
        description=description,
        type_id=type.jira_type_id,
        # Omitting searcherKey creates a field with search template None:
        # writes/reads work but every JQL clause on it 400s. Default from the
        # declared field type; callers may override.
        searcher_key=searcher_key or type.searcher_key or None,
    )
    # Persist parent before any option create — a mid-loop failure leaves the
    # parent + already-created options on disk so a re-run gives the
    # alias-already-mapped error instead of silently duplicating the field.
    ctx.state.custom_fields[alias] = CustomFieldMapping(id=field_id, options={})
    ctx.persist()
    # POST /field does not accept a `description` parameter (only name/type/
    # searcherKey), so Jira creates the field with an empty description even
    # though we sent one — and the diff re-emits a description update on the
    # next autogenerate forever. Apply it through the documented update
    # endpoint now, so a freshly-created field matches the schema immediately.
    if description:
        await ctx.engine.dialect.update_custom_field(field_id, description=description)
    for opt_value in options or []:
        opt_id = await ctx.engine.dialect.add_custom_field_option(field_id, opt_value)
        ctx.state.custom_fields[alias].options[opt_value] = opt_id
        ctx.persist()
    return field_id


async def update_custom_field(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
    searcher_key: str | None = None,
) -> None:
    """Rename, re-describe, or re-searcher an existing custom field."""
    ctx = get_context()
    mapping = _require_existing("update_custom_field", alias, ctx.state.custom_fields)
    await ctx.engine.dialect.update_custom_field(
        mapping.id,
        name=name,
        description=description,
        searcher_key=searcher_key,
    )


async def add_custom_field_option(*, field_alias: str, value: str) -> str:
    """Add an option to an existing select-style custom field. Records the
    new option id under state.custom_fields[alias].options[value]."""
    ctx = get_context()
    mapping = _require_existing(
        "add_custom_field_option",
        field_alias,
        ctx.state.custom_fields,
    )
    if value in mapping.options:
        raise ConfigurationError(
            f"add_custom_field_option: field {field_alias!r} already has "
            f"option {value!r} (id={mapping.options[value]!r})."
        )
    opt_id = await ctx.engine.dialect.add_custom_field_option(mapping.id, value)
    mapping.options[value] = opt_id
    ctx.persist()
    return opt_id


async def remove_custom_field_option(*, field_alias: str, value: str) -> None:
    """Delete an option from a select-style custom field. Destroys all uses
    of that option on existing issues; downgrade-style guard with op.unsupported
    in destructive downgrades."""
    ctx = get_context()
    mapping = _require_existing(
        "remove_custom_field_option",
        field_alias,
        ctx.state.custom_fields,
    )
    opt_id = mapping.options.get(value)
    if opt_id is None:
        raise ConfigurationError(
            f"remove_custom_field_option: field {field_alias!r} has no option "
            f"named {value!r}. Known: {sorted(mapping.options)}"
        )
    await ctx.engine.dialect.delete_custom_field_option(mapping.id, opt_id)
    del mapping.options[value]
    ctx.persist()


async def delete_custom_field(*, alias: str) -> None:
    """Delete a Jira custom field by alias. Removes the mapping from state.

    Jira destroys all data on the field. Generated downgrades wrap this with
    op.unsupported() by default. Authors can remove the guard to permit
    destruction. Idempotent: no-op when alias is absent from state.
    """
    ctx = get_context()
    mapping = ctx.state.custom_fields.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_custom_field(mapping.id)
    del ctx.state.custom_fields[alias]
    ctx.persist()


# ── Screens ──────────────────────────────────────────────────────────
async def create_screen(*, alias: str, name: str, description: str = "") -> str:
    """Create a bare screen. Tabs are added with `add_screen_tab`.
    Idempotent: returns existing id when alias is already in state."""
    ctx = get_context()
    _require_alias(alias, "create_screen")
    existing = _existing_id_or_none(ctx.state.screens, alias)
    if existing is not None:
        return existing
    screen_id = await ctx.engine.dialect.create_screen(name=name, description=description)
    ctx.state.screens[alias] = ScreenMapping(id=screen_id, tab_ids={})
    ctx.persist()
    return screen_id


async def delete_screen(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.screens.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_screen(mapping.id)
    del ctx.state.screens[alias]
    ctx.persist()


async def update_screen(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
) -> None:
    ctx = get_context()
    mapping = _require_existing("update_screen", alias, ctx.state.screens)
    await ctx.engine.dialect.update_screen(
        mapping.id,
        name=name,
        description=description,
    )


async def add_screen_tab(*, screen_alias: str, tab_name: str) -> str:
    """Add a tab to an existing screen and record its tab id.
    Idempotent: returns existing tab id if tab_name is already known."""
    ctx = get_context()
    screen = _require_existing("add_screen_tab", screen_alias, ctx.state.screens)
    if tab_name in screen.tab_ids:
        return screen.tab_ids[tab_name]
    tab_id = await ctx.engine.dialect.add_screen_tab(screen.id, name=tab_name)
    screen.tab_ids[tab_name] = tab_id
    ctx.persist()
    return tab_id


async def add_screen_tab_field(
    *,
    screen_alias: str,
    tab_name: str,
    field_alias: str,
) -> None:
    """Add a custom field (by alias) to a screen tab (by name)."""
    ctx = get_context()
    screen = _require_existing("add_screen_tab_field", screen_alias, ctx.state.screens)
    tab_id = screen.tab_ids.get(tab_name)
    if tab_id is None:
        raise ConfigurationError(
            f"add_screen_tab_field: screen {screen_alias!r} has no tab "
            f"named {tab_name!r}. Known tabs: {list(screen.tab_ids)}"
        )
    # Custom fields resolve through state by alias; system fields ("Summary",
    # "Description", etc.) are not in state and pass through as Jira field IDs
    # (lowercase, spaces stripped, matching Jira's system-field id convention).
    if field_alias in ctx.state.custom_fields:
        field_id = ctx.state.custom_fields[field_alias].id
    else:
        field_id = field_alias.replace(" ", "").lower()
    await ctx.engine.dialect.add_screen_tab_field(
        screen.id,
        tab_id,
        field_id=field_id,
    )


# ── Screen schemes ───────────────────────────────────────────────────
async def create_screen_scheme(
    *,
    alias: str,
    name: str,
    screens: Mapping[str, str],
    description: str = "",
) -> str:
    """`screens` maps Jira screen op ("default", "create", "edit", "view") to
    a screen alias. Must include "default"."""
    ctx = get_context()
    _require_alias(alias, "create_screen_scheme")
    existing = _existing_id_or_none(ctx.state.screen_schemes, alias)
    if existing is not None:
        return existing
    if "default" not in screens:
        raise ConfigurationError("create_screen_scheme: `screens` must include a 'default' entry")
    resolved: dict[str, str] = {}
    for screen_op, screen_alias in screens.items():
        screen = _require_existing(
            "create_screen_scheme",
            screen_alias,
            ctx.state.screens,
        )
        resolved[screen_op] = screen.id
    scheme_id = await ctx.engine.dialect.create_screen_scheme(
        name=name,
        description=description,
        screens=resolved,
    )
    ctx.state.screen_schemes[alias] = SimpleMapping(id=scheme_id)
    ctx.persist()
    return scheme_id


async def delete_screen_scheme(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.screen_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_screen_scheme(mapping.id)
    del ctx.state.screen_schemes[alias]
    ctx.persist()


async def update_screen_scheme(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
    screens: Mapping[str, str] | None = None,
) -> None:
    """Update name/description and/or rebind screen op→screen alias mappings."""
    ctx = get_context()
    mapping = _require_existing("update_screen_scheme", alias, ctx.state.screen_schemes)
    resolved: dict[str, str] | None = None
    if screens is not None:
        if "default" not in screens:
            raise ConfigurationError("update_screen_scheme: `screens` must include a 'default' entry")
        resolved = {}
        for screen_op, screen_alias in screens.items():
            screen = _require_existing(
                "update_screen_scheme",
                screen_alias,
                ctx.state.screens,
            )
            resolved[screen_op] = screen.id
    await ctx.engine.dialect.update_screen_scheme(
        mapping.id,
        name=name,
        description=description,
        screens=resolved,
    )


# ── Issue-type screen schemes ────────────────────────────────────────
async def create_issuetype_screen_scheme(
    *,
    alias: str,
    name: str,
    mappings: Mapping[str, str],
    description: str = "",
) -> str:
    """`mappings` maps issuetype alias → screen-scheme alias. Use "default"
    (literal) as the issuetype key for the fallback mapping; that is required.
    """
    ctx = get_context()
    _require_alias(alias, "create_issuetype_screen_scheme")
    existing = _existing_id_or_none(ctx.state.issuetype_screen_schemes, alias)
    if existing is not None:
        return existing
    if "default" not in mappings:
        raise ConfigurationError("create_issuetype_screen_scheme: `mappings` must include 'default' issuetype")
    resolved = _resolve_scheme_mappings(
        ctx,
        "create_issuetype_screen_scheme",
        mappings,
        scheme_table=ctx.state.screen_schemes,
        scheme_key="screenSchemeId",
    )
    scheme_id = await ctx.engine.dialect.create_issuetype_screen_scheme(
        name=name,
        description=description,
        mappings=resolved,
    )
    ctx.state.issuetype_screen_schemes[alias] = SimpleMapping(id=scheme_id)
    ctx.persist()
    return scheme_id


async def delete_issuetype_screen_scheme(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.issuetype_screen_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_issuetype_screen_scheme(mapping.id)
    del ctx.state.issuetype_screen_schemes[alias]
    ctx.persist()


async def update_issuetype_screen_scheme(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
    mappings: Mapping[str, str] | None = None,
) -> None:
    """Rename/re-describe and/or replace mappings on an ITSS."""
    ctx = get_context()
    mapping = _require_existing(
        "update_issuetype_screen_scheme",
        alias,
        ctx.state.issuetype_screen_schemes,
    )
    if name is not None or description is not None:
        await ctx.engine.dialect.update_issuetype_screen_scheme(
            mapping.id,
            name=name,
            description=description,
        )
    if mappings is not None:
        if "default" not in mappings:
            raise ConfigurationError("update_issuetype_screen_scheme: `mappings` must include 'default' issuetype")
        resolved = _resolve_scheme_mappings(
            ctx,
            "update_issuetype_screen_scheme",
            mappings,
            scheme_table=ctx.state.screen_schemes,
            scheme_key="screenSchemeId",
        )
        await ctx.engine.dialect.set_issuetype_screen_scheme_mappings(
            mapping.id,
            mappings=resolved,
        )


# ── Field configurations ─────────────────────────────────────────────
async def create_field_configuration(
    *,
    alias: str,
    name: str,
    description: str = "",
) -> str:
    ctx = get_context()
    _require_alias(alias, "create_field_configuration")
    existing = _existing_id_or_none(ctx.state.field_configurations, alias)
    if existing is not None:
        return existing
    fc_id = await ctx.engine.dialect.create_field_configuration(
        name=name,
        description=description,
    )
    ctx.state.field_configurations[alias] = SimpleMapping(id=fc_id)
    ctx.persist()
    return fc_id


async def delete_field_configuration(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.field_configurations.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_field_configuration(mapping.id)
    del ctx.state.field_configurations[alias]
    ctx.persist()


async def update_field_configuration(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
) -> None:
    """Rename or re-describe an existing field configuration / field scheme."""
    ctx = get_context()
    mapping = _require_existing("update_field_configuration", alias, ctx.state.field_configurations)
    await ctx.engine.dialect.update_field_configuration(
        mapping.id,
        name=name,
        description=description,
    )


async def set_field_configuration_item(
    *,
    fc_alias: str,
    field_alias: str,
    required: bool = False,
    hidden: bool = False,
    description: str = "",
) -> None:
    ctx = get_context()
    fc = _require_existing(
        "set_field_configuration_item",
        fc_alias,
        ctx.state.field_configurations,
    )
    if field_alias in ctx.state.custom_fields:
        field_id = ctx.state.custom_fields[field_alias].id
    else:
        field_id = field_alias.replace(" ", "").lower()
    await ctx.engine.dialect.set_field_configuration_item(
        fc.id,
        field_id=field_id,
        required=required,
        hidden=hidden,
        description=description,
    )


# ── Field configuration schemes ──────────────────────────────────────
async def create_field_configuration_scheme(
    *,
    alias: str,
    name: str,
    mappings: Mapping[str, str],
    description: str = "",
) -> str:
    """`mappings` maps issuetype alias → field-configuration alias. Must
    include "default" (literal) as a fallback."""
    ctx = get_context()
    _require_alias(alias, "create_field_configuration_scheme")
    existing = _existing_id_or_none(ctx.state.field_configuration_schemes, alias)
    if existing is not None:
        return existing
    if "default" not in mappings:
        raise ConfigurationError("create_field_configuration_scheme: `mappings` must include 'default' issuetype")
    resolved = _resolve_scheme_mappings(
        ctx,
        "create_field_configuration_scheme",
        mappings,
        scheme_table=ctx.state.field_configurations,
        scheme_key="fieldConfigurationId",
    )
    scheme_id = await ctx.engine.dialect.create_field_configuration_scheme(
        name=name,
        description=description,
    )
    # Persist the FCS id before the mappings PUT so a network failure on the
    # PUT doesn't leak an untracked FCS into Jira.
    ctx.state.field_configuration_schemes[alias] = SimpleMapping(id=scheme_id)
    ctx.persist()
    await ctx.engine.dialect.set_field_configuration_scheme_mappings(
        scheme_id,
        mappings=resolved,
    )
    return scheme_id


async def delete_field_configuration_scheme(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.field_configuration_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_field_configuration_scheme(mapping.id)
    del ctx.state.field_configuration_schemes[alias]
    ctx.persist()


async def update_field_configuration_scheme(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
    mappings: Mapping[str, str] | None = None,
) -> None:
    """Rename/re-describe and/or replace mappings on an FCS."""
    ctx = get_context()
    mapping = _require_existing(
        "update_field_configuration_scheme",
        alias,
        ctx.state.field_configuration_schemes,
    )
    if name is not None or description is not None:
        await ctx.engine.dialect.update_field_configuration_scheme(
            mapping.id,
            name=name,
            description=description,
        )
    if mappings is not None:
        if "default" not in mappings:
            raise ConfigurationError("update_field_configuration_scheme: `mappings` must include 'default' issuetype")
        resolved = _resolve_scheme_mappings(
            ctx,
            "update_field_configuration_scheme",
            mappings,
            scheme_table=ctx.state.field_configurations,
            scheme_key="fieldConfigurationId",
        )
        await ctx.engine.dialect.set_field_configuration_scheme_mappings(
            mapping.id,
            mappings=resolved,
        )


# ── Issue types ──────────────────────────────────────────────────────
async def create_issuetype(
    *,
    alias: str,
    name: str,
    description: str = "",
    subtask: bool = False,
) -> str:
    ctx = get_context()
    _require_alias(alias, "create_issuetype")
    existing = _existing_id_or_none(ctx.state.issuetypes, alias)
    if existing is not None:
        return existing
    # Every Jira tenant ships built-in issue types (Bug, Task, Story, ...) and
    # enforces globally-unique names, so POSTing a duplicate 409s. Adopt the
    # existing type by name into state instead of recreating it (#8).
    adopted = await ctx.engine.dialect.find_issuetype_id_by_name(name)
    if adopted is not None:
        ctx.state.issuetypes[alias] = SimpleMapping(id=adopted)
        ctx.persist()
        return adopted
    it_id = await ctx.engine.dialect.create_issuetype(
        name=name,
        description=description,
        subtask=subtask,
    )
    ctx.state.issuetypes[alias] = SimpleMapping(id=it_id)
    ctx.persist()
    return it_id


async def delete_issuetype(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.issuetypes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_issuetype(mapping.id)
    del ctx.state.issuetypes[alias]
    ctx.persist()


async def update_issuetype(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
) -> None:
    ctx = get_context()
    mapping = _require_existing("update_issuetype", alias, ctx.state.issuetypes)
    await ctx.engine.dialect.update_issuetype(
        mapping.id,
        name=name,
        description=description,
    )


# ── Projects ─────────────────────────────────────────────────────────
async def create_project(
    *,
    alias: str,
    key: str,
    name: str,
    project_type_key: str,
    lead: str,
    description: str = "",
    project_template_key: str | None = None,
    style: str = "company-managed",
) -> str:
    """Create a Jira project. ``style`` selects CMP vs TMP (Cloud only).

    On Cloud, TMP defaults a sensible project_template_key when one is not
    given (Atlassian's "next-gen" Kanban template). CMP now defaults too:
    **POST /project silently defaults to the same team-managed template when
    ``projectTemplateKey`` is omitted**, and a team-managed project ignores
    the entire scheme family (screens, ITSS, security schemes associate but
    no-op) — so an unplanned CMP create silently built a project the data
    plane couldn't use (the ``SEC`` incident: every create 400'd with "not
    on the appropriate screen" despite the migration chaining perfectly).

    Templates are matched to the declared style: a known team-managed
    template with ``style="company-managed"`` (or vice versa) raises before
    any wire call.
    """
    ctx = get_context()
    _require_alias(alias, "create_project")
    existing = _existing_id_or_none(ctx.state.projects, alias)
    if existing is not None:
        return existing
    if style not in ("company-managed", "team-managed"):
        raise ConfigurationError(
            f"create_project: unknown style {style!r}; expected 'company-managed' or 'team-managed'"
        )
    _default_tmp_template_key = "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban"
    _default_cmp_template_key = "com.pyxis.greenhopper.jira:gh-kanban-template"
    if style == "team-managed" and project_template_key is None:
        project_template_key = _default_tmp_template_key
    if style == "company-managed" and project_template_key is None:
        project_template_key = _default_cmp_template_key
    # Template/style mismatch is unforgeable at apply time: a project created
    # on the wrong template ignores its schemes entirely, and every later op
    # (and the style recorded in state) drifts forever.
    template_style = "team-managed" if "simplified-agility" in (project_template_key or "") else "company-managed"
    if project_template_key and template_style != style:
        raise ConfigurationError(
            f"create_project: template {project_template_key!r} creates a "
            f"{template_style} project but the schema declares style={style!r}. "
            f"Pass a matching project_template_key (for company-managed, "
            f"e.g. '{_default_cmp_template_key}')."
        )
    resolved_lead = await ctx.engine.dialect.resolve_lead(lead)
    proj_id = await ctx.engine.dialect.create_project(
        key=key,
        name=name,
        project_type_key=project_type_key,
        lead=resolved_lead,
        description=description,
        project_template_key=project_template_key,
    )
    ctx.state.projects[alias] = ProjectMapping(id=proj_id, style=style, key=key)
    ctx.persist()
    return proj_id


async def update_project(
    *,
    alias: str,
    name: str | None = None,
    lead: str | None = None,
    description: str | None = None,
) -> None:
    """Rename or re-describe an existing project, or change its lead."""
    ctx = get_context()
    mapping = _require_existing("update_project", alias, ctx.state.projects)
    resolved_lead = await ctx.engine.dialect.resolve_lead(lead) if lead is not None else None
    await ctx.engine.dialect.update_project(
        mapping.id,
        name=name,
        lead=resolved_lead,
        description=description,
    )


async def delete_project(*, alias: str, key: str) -> None:
    """Delete takes both id and key; Jira Cloud deletes by id. The key stays
    part of the stable op surface. Idempotent: no-op when alias is absent from
    state."""
    ctx = get_context()
    mapping = ctx.state.projects.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_project(project_id=mapping.id, project_key=key)
    del ctx.state.projects[alias]
    ctx.persist()


async def set_project_issuetype_screen_scheme(
    *,
    project_alias: str,
    scheme_alias: str,
) -> None:
    ctx = get_context()
    project = _require_existing(
        "set_project_issuetype_screen_scheme",
        project_alias,
        ctx.state.projects,
    )
    _refuse_tmp(
        project,
        project_alias,
        "set_project_issuetype_screen_scheme",
        ctx.state.jira_url,
    )
    scheme = _require_existing(
        "set_project_issuetype_screen_scheme",
        scheme_alias,
        ctx.state.issuetype_screen_schemes,
    )
    await ctx.engine.dialect.set_project_issuetype_screen_scheme(
        project_id=project.id,
        scheme_id=scheme.id,
    )


async def set_project_field_configuration_scheme(
    *,
    project_alias: str,
    scheme_alias: str,
) -> None:
    ctx = get_context()
    project = _require_existing(
        "set_project_field_configuration_scheme",
        project_alias,
        ctx.state.projects,
    )
    _refuse_tmp(
        project,
        project_alias,
        "set_project_field_configuration_scheme",
        ctx.state.jira_url,
    )
    scheme = _require_existing(
        "set_project_field_configuration_scheme",
        scheme_alias,
        ctx.state.field_configuration_schemes,
    )
    await ctx.engine.dialect.set_project_field_configuration_scheme(
        project_id=project.id,
        scheme_id=scheme.id,
    )


# ── Issue type schemes ───────────────────────────────────────────────
async def create_issuetype_scheme(
    *,
    alias: str,
    name: str,
    issuetypes: list[str],
    default_issuetype: str,
    description: str = "",
) -> str:
    """``issuetypes`` is a list of issuetype aliases. ``default_issuetype`` is
    the alias used as the scheme's default; it must be in ``issuetypes``.
    Jira additionally requires at least one *standard* (non-subtask) issuetype
    in the list — the dialect surfaces that as a 400 if violated.
    """
    ctx = get_context()
    _require_alias(alias, "create_issuetype_scheme")
    existing = _existing_id_or_none(ctx.state.issuetype_schemes, alias)
    if existing is not None:
        return existing
    if not issuetypes:
        raise ConfigurationError("create_issuetype_scheme: `issuetypes` must be non-empty")
    if default_issuetype not in issuetypes:
        raise ConfigurationError(
            f"create_issuetype_scheme: default_issuetype {default_issuetype!r} must be one of {list(issuetypes)}"
        )
    issuetype_ids: list[str] = []
    for it_alias in issuetypes:
        m = _require_existing("create_issuetype_scheme", it_alias, ctx.state.issuetypes)
        issuetype_ids.append(m.id)
    default_id = ctx.state.issuetypes[default_issuetype].id
    scheme_id = await ctx.engine.dialect.create_issuetype_scheme(
        name=name,
        description=description,
        issuetype_ids=issuetype_ids,
        default_issuetype_id=default_id,
    )
    ctx.state.issuetype_schemes[alias] = SimpleMapping(id=scheme_id)
    ctx.persist()
    return scheme_id


async def update_issuetype_scheme(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
    default_issuetype: str | None = None,
    issuetypes: list[str] | None = None,
) -> None:
    """``issuetypes`` (when given) replaces the scheme's full member list;
    each entry is an issuetype alias resolved through ``state.issuetypes``.
    Members Jira holds that are not in the list are removed (Jira itself
    400s removals it cannot perform); the declared order is reconciled via
    the move endpoint.
    """
    ctx = get_context()
    mapping = _require_existing("update_issuetype_scheme", alias, ctx.state.issuetype_schemes)
    if issuetypes is not None:
        if not issuetypes:
            raise ConfigurationError("update_issuetype_scheme: `issuetypes` must be non-empty")
        if default_issuetype is not None and default_issuetype not in issuetypes:
            raise ConfigurationError(
                f"update_issuetype_scheme: default_issuetype {default_issuetype!r} must be one of {list(issuetypes)}"
            )
        issuetype_ids: list[str] | None = []
        for it_alias in issuetypes:
            m = _require_existing("update_issuetype_scheme", it_alias, ctx.state.issuetypes)
            issuetype_ids.append(m.id)
    else:
        issuetype_ids = None
    default_id: str | None = None
    if default_issuetype is not None:
        default_id = _require_existing(
            "update_issuetype_scheme",
            default_issuetype,
            ctx.state.issuetypes,
        ).id
    # Membership first: Jira requires the default issuetype to be a member of
    # the scheme, so a newly added default must land before the header update.
    if issuetype_ids is not None:
        await ctx.engine.dialect.set_issuetype_scheme_issuetypes(
            mapping.id,
            issuetype_ids=issuetype_ids,
        )
    if name is not None or description is not None or default_id is not None:
        await ctx.engine.dialect.update_issuetype_scheme(
            mapping.id,
            name=name,
            description=description,
            default_issuetype_id=default_id,
        )


async def delete_issuetype_scheme(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.issuetype_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_issuetype_scheme(mapping.id)
    del ctx.state.issuetype_schemes[alias]
    ctx.persist()


async def set_project_issuetype_scheme(
    *,
    project_alias: str,
    scheme_alias: str,
) -> None:
    ctx = get_context()
    project = _require_existing(
        "set_project_issuetype_scheme",
        project_alias,
        ctx.state.projects,
    )
    _refuse_tmp(
        project,
        project_alias,
        "set_project_issuetype_scheme",
        ctx.state.jira_url,
    )
    scheme = _require_existing(
        "set_project_issuetype_scheme",
        scheme_alias,
        ctx.state.issuetype_schemes,
    )
    await ctx.engine.dialect.set_project_issuetype_scheme(
        project_id=project.id,
        scheme_id=scheme.id,
    )


# ── Issue security schemes ───────────────────────────────────────────
async def create_issue_security_scheme(
    *,
    alias: str,
    name: str,
    description: str = "",
    levels: Iterable[Mapping[str, Any]] | None = None,
) -> str:
    """Create an issue security scheme with inline levels.

    ``levels`` entries carry the declared shape:
    ``dict(alias=..., name=..., description=..., is_default=...)``.
    Member-bearing levels use ``op.add_issue_security_level_member`` after the
    scheme exists (the create endpoint accepts members inline, but resolving
    user emails to accountIds happens per-member, and the not-yet-scheme-id'd
    levels can't be tracked before the create returns anyway).

    Returns the new Jira scheme id. Idempotent: re-run returns the existing
    id; missing levels from the declaration are filled in (mirroring
    ``create_custom_field``'s option-fill behavior)."""
    ctx = get_context()
    _require_alias(alias, "create_issue_security_scheme")
    existing = _existing_id_or_none(ctx.state.security_schemes, alias)
    if existing is not None:
        declared = {entry["alias"]: entry for entry in levels or []}
        known = ctx.state.security_schemes[alias].level_ids
        for entry_alias, entry in declared.items():
            if entry_alias not in known:
                await _create_level_and_record(
                    ctx,
                    alias,
                    entry_alias,
                    name=str(entry.get("name", "")),
                    description=str(entry.get("description", "")),
                    is_default=bool(entry.get("is_default", False)),
                )
        return existing
    body_levels: list[dict[str, Any]] = []
    for entry in levels or []:
        body_levels.append(
            {
                "name": entry.get("name", ""),
                "description": entry.get("description", ""),
                "isDefault": bool(entry.get("is_default", False)),
            }
        )
    scheme_id = await ctx.engine.dialect.create_issue_security_scheme(
        name=name,
        description=description,
        levels=body_levels or None,
    )
    mapping = SecuritySchemeMapping(id=scheme_id)
    ctx.state.security_schemes[alias] = mapping
    ctx.persist()
    await _capture_level_ids(ctx, alias, mapping, {e.get("alias", ""): e for e in levels or []})
    return scheme_id


async def update_issue_security_scheme(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
) -> None:
    ctx = get_context()
    mapping = _require_existing("update_issue_security_scheme", alias, ctx.state.security_schemes)
    await ctx.engine.dialect.update_issue_security_scheme(
        mapping.id,
        name=name,
        description=description,
    )


async def add_issue_security_level(
    *,
    scheme_alias: str,
    level_alias: str,
    name: str,
    description: str = "",
    is_default: bool = False,
) -> None:
    """Add one level to an existing scheme. Persists the level id captured by
    a follow-up GET (the add-level endpoint returns 204 with no body)."""
    ctx = get_context()
    mapping = _require_existing("add_issue_security_level", scheme_alias, ctx.state.security_schemes)
    if level_alias in mapping.level_ids:
        return  # idempotent re-run
    await ctx.engine.dialect.add_issue_security_levels(
        mapping.id,
        levels=[{"name": name, "description": description, "isDefault": is_default}],
    )
    await _capture_level_ids(
        ctx,
        scheme_alias,
        mapping,
        {level_alias: {"name": name, "description": description, "isDefault": is_default}},
    )


async def add_issue_security_level_member(
    *,
    scheme_alias: str,
    level_alias: str,
    member_type: str,
    member_value: str,
) -> str:
    """Add a member to an existing level.

    ``member_value`` is a group name, or (for users) an email or raw
    accountId; emails resolve through the user-search API. Records the
    *resolved* parameter in state so re-runs stay idempotent even when the
    declared value is an email the reflection payload will only ever carry
    as an accountId.
    """
    ctx = get_context()
    mapping = _require_existing("add_issue_security_level_member", scheme_alias, ctx.state.security_schemes)
    members = mapping.member_values.setdefault(level_alias, [])
    if member_value in members:
        return member_value  # idempotent re-run with the resolved value
    level_id = mapping.level_ids.get(level_alias)
    if level_id is None:
        raise ConfigurationError(
            f"add_issue_security_level_member: scheme {scheme_alias!r} has no "
            f"tracked level {level_alias!r}. Known levels: {sorted(mapping.level_ids)}"
        )
    # Resolve *before* the recorded check: state stores resolved parameters,
    # so a re-run of a member declared as an email must compare its resolved
    # form — a raw email versus recorded accountIds never matches and would
    # re-send the add every upgrade.
    parameter = await ctx.engine.dialect.resolve_security_level_member(
        member_type=member_type,
        member_value=member_value,
    )
    if parameter in members:
        return parameter  # idempotent re-run with the declared value
    await ctx.engine.dialect.add_issue_security_level_member(
        scheme_id=mapping.id,
        level_id=level_id,
        member_type=member_type,
        member_id_or_name=parameter,
    )
    members.append(parameter)
    ctx.persist()
    return parameter


async def set_default_issue_security_level(*, scheme_alias: str, level_alias: str) -> None:
    ctx = get_context()
    mapping = _require_existing("set_default_issue_security_level", scheme_alias, ctx.state.security_schemes)
    level_id = mapping.level_ids.get(level_alias)
    if level_id is None:
        raise ConfigurationError(
            f"set_default_issue_security_level: scheme {scheme_alias!r} has no "
            f"tracked level {level_alias!r}. Known levels: {sorted(mapping.level_ids)}"
        )
    await ctx.engine.dialect.set_default_issue_security_levels({mapping.id: level_id})
    mapping.default_level_id = level_id
    ctx.persist()


async def clear_default_issue_security_level(*, scheme_alias: str) -> None:
    """Reset a scheme's default level to none.

    New issues then carry no issue-security level. This is the only way to
    make a scheme's issues visible when its current default level has no
    members: such issues 404 on GET/PUT and never appear in JQL, even for
    the token that created them."""
    ctx = get_context()
    mapping = _require_existing("clear_default_issue_security_level", scheme_alias, ctx.state.security_schemes)
    await ctx.engine.dialect.set_default_issue_security_levels({mapping.id: None})
    mapping.default_level_id = None
    ctx.persist()


async def set_project_issue_security_scheme(*, project_alias: str, scheme_alias: str) -> None:
    """Associate a scheme with a project.

    Jira runs the association (and any issue remapping) asynchronously: the
    PUT answers 303 with a task bean. Poll the task to completion with a
    bounded wait so a failed association fails the migration instead of
    succeeding on the wire (and leaving the op untracked-without-binding)."""
    ctx = get_context()
    project = _require_existing("set_project_issue_security_scheme", project_alias, ctx.state.projects)
    _refuse_tmp(
        project,
        project_alias,
        "set_project_issue_security_scheme",
        ctx.state.jira_url,
    )
    scheme = _require_existing(
        "set_project_issue_security_scheme",
        scheme_alias,
        ctx.state.security_schemes,
    )
    task = await ctx.engine.dialect.associate_security_scheme_to_project(
        project_id=project.id,
        scheme_id=scheme.id,
    )
    await _await_security_task(ctx.engine.dialect, task)


_ASSOCIATE_POLL_ATTEMPTS = 30
_ASSOCIATE_POLL_DELAY = 2.0
_TASK_TERMINAL_FAILURE = ("FAILED", "CANCELLED", "KILLED", "ERROR", "DEAD")


async def _await_security_task(dialect: Any, task: Any) -> None:
    """Poll an associate task bean to a terminal state, bounded.

    An immediate COMPLETE (common for small projects) never polls. A terminal
    failure raises. Runs out of attempts → a StintError telling the operator
    where the task endpoint lives, so the run is visibly wrong rather than
    silently-green on an unbound project.
    """
    from stint.exceptions import StintError

    if not isinstance(task, dict):
        return  # defensive: mock/stub responses without a task bean
    status = str(task.get("status", "COMPLETE")).upper()
    if status in _TASK_TERMINAL_FAILURE:
        raise StintError(f"security-scheme association task failed: {str(task)[:200]}")
    if status not in ("ENQUEUED", "RUNNING"):
        return
    poll_path = task.get("self") or task.get("location")
    if not poll_path:
        raise StintError(
            f"security-scheme association is not complete ({status}) and the "
            f"response carries no self/location link to poll; the task status "
            f"must be checked manually: {str(task)[:200]}"
        )
    for _ in range(_ASSOCIATE_POLL_ATTEMPTS):
        await asyncio.sleep(_ASSOCIATE_POLL_DELAY)
        task = await dialect.client.get_json(str(poll_path))
        if not isinstance(task, dict):
            continue
        status = str(task.get("status", "")).upper()
        if status in _TASK_TERMINAL_FAILURE:
            raise StintError(f"security-scheme association task failed: {str(task)[:200]}")
        if status not in ("ENQUEUED", "RUNNING"):
            return
    raise StintError(
        f"security-scheme association task did not finish within "
        f"{_ASSOCIATE_POLL_ATTEMPTS * _ASSOCIATE_POLL_DELAY:.0f}s; check its "
        f"status at {poll_path} and re-run the migration if it failed."
    )


async def delete_issue_security_scheme(*, alias: str) -> None:
    """Delete the scheme by id. Idempotent: no-op when absent from state.

    Jira refuses to delete a scheme still associated with a project — the
    emitted delete order handles that (DeleteProject → scheme deletes), and
    a hand-written migration must unbind first (its 400 error surfaces here).
    """
    ctx = get_context()
    mapping = ctx.state.security_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_issue_security_scheme(mapping.id)
    del ctx.state.security_schemes[alias]
    ctx.persist()


async def _capture_level_ids(
    ctx: Any,
    scheme_alias: str,
    mapping: SecuritySchemeMapping,
    declared: Mapping[str, Mapping[str, Any]],
) -> None:
    """Fill ``level_ids``/``default_level_id`` from the live scheme.

    Jira's create/add-level responses carry no level ids, so one GET after
    the write captures them. Levels are matched to declarations by level
    name (unique within a scheme).
    """
    if not declared:
        return
    levels = await ctx.engine.dialect.get_issue_security_levels(mapping.id)
    by_name: dict[str, tuple[str, bool]] = {}
    for lv in levels:
        by_name[lv.name] = (lv.id, lv.is_default)
        if lv.is_default:
            mapping.default_level_id = lv.id
    for level_alias, entry in declared.items():
        lv = by_name.get(str(entry.get("name", "")))
        if lv is not None:
            mapping.level_ids[level_alias] = lv[0]
            if lv[1]:
                mapping.default_level_id = lv[0]
    ctx.persist()


async def _create_level_and_record(
    ctx: Any,
    scheme_alias: str,
    level_alias: str,
    *,
    name: str,
    description: str,
    is_default: bool,
) -> None:
    mapping = ctx.state.security_schemes[scheme_alias]
    if level_alias not in mapping.level_ids:
        await ctx.engine.dialect.add_issue_security_levels(
            mapping.id,
            levels=[{"name": name, "description": description, "isDefault": is_default}],
        )
        await _capture_level_ids(
            ctx,
            scheme_alias,
            mapping,
            {level_alias: {"name": name, "description": description, "isDefault": is_default}},
        )


# ── Escape hatch ─────────────────────────────────────────────────────
def unsupported(reason: str) -> None:
    """Signal that the surrounding migration cannot run safely.

    Use in downgrade() bodies for ops that would destroy data. Raises
    UnsupportedDowngradeError so the runner aborts cleanly.
    """
    raise UnsupportedDowngradeError(reason)


# ── Internal helpers ─────────────────────────────────────────────────
def _require_alias(alias: str, op_name: str) -> None:
    if not alias:
        raise ConfigurationError(f"{op_name} requires a non-empty alias")


def _refuse_existing(
    op_name: str,
    alias: str,
    table: Mapping[str, Any],
) -> None:
    if alias in table:
        raise ConfigurationError(
            f"{op_name}: alias {alias!r} already mapped to {table[alias].id!r} in state. Use update_* or delete first."
        )


def _existing_id_or_none(table: Mapping[str, Any], alias: str) -> str | None:
    """Return the existing id if alias is in state, else None. Used by
    create ops for idempotent re-run support."""
    mapping = table.get(alias)
    return mapping.id if mapping is not None else None


def _refuse_tmp(
    project: Any,
    project_alias: str,
    op_name: str,
    jira_url: str,
) -> None:
    """Raise UnsupportedTMPOpError if the target project is team-managed.

    Many CMP-style ops (scheme binding, screen scheme assignment) have no
    REST equivalent on TMP — Atlassian gates them to the project's "settings"
    UI. We surface that with a deep link so the user can finish manually.
    """
    from stint.exceptions import UnsupportedTMPOpError

    if getattr(project, "style", "company-managed") != "team-managed":
        return
    key = getattr(project, "key", "") or project_alias
    deep_link = f"{jira_url.rstrip('/')}/jira/software/projects/{key}/settings/details"
    raise UnsupportedTMPOpError(
        f"{op_name}: project {project_alias!r} is team-managed. CMP-style "
        f"scheme bindings have no REST equivalent on TMP. Complete this "
        f"change in the Jira UI: {deep_link}"
    )


def _require_existing(op_name: str, alias: str, table: Mapping[str, Any]) -> Any:
    mapping = table.get(alias)
    if mapping is None:
        raise ConfigurationError(f"{op_name}: alias {alias!r} not present in state. Known: {sorted(table)}")
    return mapping


def _resolve_scheme_mappings(
    ctx: Any,
    op_name: str,
    mappings: Mapping[str, str],
    *,
    scheme_table: Mapping[str, Any],
    scheme_key: str,
) -> list[dict[str, str]]:
    """Translate alias→alias mappings into Jira's [{issueTypeId, <key>: id}] shape.

    "default" (the literal string) is reserved as an issuetype alias and is
    passed through unchanged; other issuetype aliases must already be in state.
    """
    out: list[dict[str, str]] = []
    for it_alias, scheme_alias in mappings.items():
        if it_alias == "default":
            it_id = "default"
        else:
            it = _require_existing(op_name, it_alias, ctx.state.issuetypes)
            it_id = it.id
        scheme = _require_existing(op_name, scheme_alias, scheme_table)
        out.append({"issueTypeId": it_id, scheme_key: scheme.id})
    return out
