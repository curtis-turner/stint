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

from collections.abc import Iterable, Mapping
from typing import Any

from stint.exceptions import ConfigurationError
from stint.fields import _FieldType
from stint.migrations.context import get_context
from stint.migrations.exceptions import UnsupportedDowngradeError
from stint.state.file import CustomFieldMapping, ProjectMapping, ScreenMapping, SimpleMapping


# ── Custom fields ────────────────────────────────────────────────────
async def create_custom_field(
    *,
    alias: str,
    name: str,
    type: type[_FieldType],
    description: str = "",
    options: Iterable[str] | None = None,
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
    )
    # Persist parent before any option create — a mid-loop failure leaves the
    # parent + already-created options on disk so a re-run gives the
    # alias-already-mapped error instead of silently duplicating the field.
    ctx.state.custom_fields[alias] = CustomFieldMapping(id=field_id, options={})
    ctx.persist()
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
) -> None:
    """Rename or re-describe an existing custom field. id+options unchanged."""
    ctx = get_context()
    mapping = _require_existing("update_custom_field", alias, ctx.state.custom_fields)
    await ctx.engine.dialect.update_custom_field(
        mapping.id,
        name=name,
        description=description,
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
    return screen_id


async def delete_screen(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.screens.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_screen(mapping.id)
    del ctx.state.screens[alias]


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
    return scheme_id


async def delete_screen_scheme(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.screen_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_screen_scheme(mapping.id)
    del ctx.state.screen_schemes[alias]


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
    return scheme_id


async def delete_issuetype_screen_scheme(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.issuetype_screen_schemes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_issuetype_screen_scheme(mapping.id)
    del ctx.state.issuetype_screen_schemes[alias]


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
    return fc_id


async def delete_field_configuration(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.field_configurations.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_field_configuration(mapping.id)
    del ctx.state.field_configurations[alias]


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
    it_id = await ctx.engine.dialect.create_issuetype(
        name=name,
        description=description,
        subtask=subtask,
    )
    ctx.state.issuetypes[alias] = SimpleMapping(id=it_id)
    return it_id


async def delete_issuetype(*, alias: str) -> None:
    """Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.issuetypes.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_issuetype(mapping.id)
    del ctx.state.issuetypes[alias]


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
    given (Atlassian's "next-gen" Kanban template).
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
    if style == "team-managed" and project_template_key is None:
        # Atlassian's default TMP Kanban template
        project_template_key = "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban"
    proj_id = await ctx.engine.dialect.create_project(
        key=key,
        name=name,
        project_type_key=project_type_key,
        lead=lead,
        description=description,
        project_template_key=project_template_key,
    )
    ctx.state.projects[alias] = ProjectMapping(id=proj_id, style=style, key=key)
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
    await ctx.engine.dialect.update_project(
        mapping.id,
        name=name,
        lead=lead,
        description=description,
    )


async def delete_project(*, alias: str, key: str) -> None:
    """Delete requires the key because DC's delete URL is keyed by project key,
    while Cloud uses id. Idempotent: no-op when alias is absent from state."""
    ctx = get_context()
    mapping = ctx.state.projects.get(alias)
    if mapping is None:
        return
    await ctx.engine.dialect.delete_project(project_id=mapping.id, project_key=key)
    del ctx.state.projects[alias]


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
    return scheme_id


async def update_issuetype_scheme(
    *,
    alias: str,
    name: str | None = None,
    description: str | None = None,
    default_issuetype: str | None = None,
) -> None:
    ctx = get_context()
    mapping = _require_existing("update_issuetype_scheme", alias, ctx.state.issuetype_schemes)
    default_id: str | None = None
    if default_issuetype is not None:
        default_id = _require_existing(
            "update_issuetype_scheme",
            default_issuetype,
            ctx.state.issuetypes,
        ).id
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
