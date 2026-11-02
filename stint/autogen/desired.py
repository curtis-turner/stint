"""Build a DesiredSnapshot from the schema registry.

The desired snapshot has the same shape as the reflected Snapshot but is built
from Pydantic class declarations rather than HTTP reflection. The diff
algorithm compares the two.

ITSS and FCS are derived per-project: each Project gets one synthesized ITSS
(alias `{project_key}_itss`) and one synthesized FCS (alias `{project_key}_fcs`),
with `default` mapped to the first issuetype's screen scheme / field config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stint.fields import CustomField
from stint.registry import registry as default_registry

if TYPE_CHECKING:
    from stint.registry import Registry
    from stint.schema.field_config import FieldConfiguration
    from stint.schema.project import Project
    from stint.schema.screen import Screen, ScreenScheme


@dataclass(frozen=True)
class DesiredCustomField:
    alias: str
    name: str
    type_id: str
    description: str
    options: tuple[str, ...]  # ordered
    searcher_key: str = ""


@dataclass(frozen=True)
class DesiredIssueType:
    alias: str
    name: str
    description: str
    subtask: bool = False


@dataclass(frozen=True)
class DesiredScreenTab:
    """One tab per screen for now: 'Fields'. Field order preserved."""

    name: str
    field_refs: tuple[str, ...]  # system field names OR custom-field aliases


@dataclass(frozen=True)
class DesiredScreen:
    alias: str
    name: str
    description: str
    tabs: tuple[DesiredScreenTab, ...]


@dataclass(frozen=True)
class DesiredScreenScheme:
    alias: str
    name: str
    description: str
    screens: dict[str, str]  # op ("default","create","edit","view") -> screen alias


@dataclass(frozen=True)
class DesiredFieldConfigurationItem:
    field_alias: str
    required: bool = False
    hidden: bool = False


@dataclass(frozen=True)
class DesiredFieldConfiguration:
    alias: str
    name: str
    description: str
    items: tuple[DesiredFieldConfigurationItem, ...]


@dataclass(frozen=True)
class DesiredIssueTypeScreenScheme:
    alias: str
    name: str
    description: str
    # issuetype alias (or "default") -> screen-scheme alias
    mappings: dict[str, str]


@dataclass(frozen=True)
class DesiredIssueTypeScheme:
    """An IssueTypeScheme: the set of issuetypes available in a project. Stint
    synthesizes one per Project from its ``__issuetypes__`` list."""

    alias: str
    name: str
    description: str
    issuetypes: tuple[str, ...]  # ordered issuetype aliases
    default_issuetype: str  # one of the entries in ``issuetypes``


@dataclass(frozen=True)
class DesiredFieldConfigurationScheme:
    alias: str
    name: str
    description: str
    # issuetype alias (or "default") -> field-configuration alias
    mappings: dict[str, str]


@dataclass(frozen=True)
class DesiredSecurityLevelMember:
    type: str  # "user" or "group"
    value: str  # group name, user email, or raw accountId (holder.parameter)


@dataclass(frozen=True)
class DesiredSecurityLevel:
    alias: str  # declaration designation (SecurityLevel.id, or name)
    name: str  # Jira level name
    description: str
    is_default: bool
    members: tuple[DesiredSecurityLevelMember, ...]


@dataclass(frozen=True)
class DesiredSecurityScheme:
    alias: str
    name: str
    description: str
    levels: tuple[DesiredSecurityLevel, ...]  # declaration order
    default_level: str | None  # level alias, or None
    project: str | None  # project key (alias) hint from Project.__security_scheme__


@dataclass(frozen=True)
class DesiredProject:
    alias: str  # uses project key
    key: str
    name: str
    project_type_key: str
    lead: str
    description: str
    style: str  # "company-managed" or "team-managed"
    issuetype_scheme: str | None  # alias
    issuetype_screen_scheme: str | None  # alias
    field_configuration_scheme: str | None  # alias
    security_scheme: str | None  # alias


@dataclass
class DesiredSnapshot:
    custom_fields: dict[str, DesiredCustomField] = field(default_factory=dict)
    issuetypes: dict[str, DesiredIssueType] = field(default_factory=dict)
    screens: dict[str, DesiredScreen] = field(default_factory=dict)
    screen_schemes: dict[str, DesiredScreenScheme] = field(default_factory=dict)
    issuetype_schemes: dict[str, DesiredIssueTypeScheme] = field(default_factory=dict)
    issuetype_screen_schemes: dict[str, DesiredIssueTypeScreenScheme] = field(
        default_factory=dict,
    )
    field_configurations: dict[str, DesiredFieldConfiguration] = field(
        default_factory=dict,
    )
    field_configuration_schemes: dict[str, DesiredFieldConfigurationScheme] = field(
        default_factory=dict,
    )
    security_schemes: dict[str, DesiredSecurityScheme] = field(default_factory=dict)
    projects: dict[str, DesiredProject] = field(default_factory=dict)


SYSTEM_FIELD_NAMES = {
    "Summary",
    "Description",
    "Reporter",
    "Assignee",
    "Priority",
    "Created",
    "Updated",
    "Status",
    "Resolution",
    "Labels",
    "Components",
    "Fix Version/s",
    "Affects Version/s",
    "Due Date",
    "Environment",
    "Issue Type",
}


@dataclass(frozen=True)
class _Scope:
    """Which registry entries a desired snapshot may include."""

    projects: dict[str, type[Project]]
    issuetypes: frozenset[str]  # aliases
    custom_fields: frozenset[str]  # aliases
    screens: frozenset[str]  # aliases
    screen_schemes: frozenset[str]  # aliases
    field_configurations: frozenset[str]  # aliases
    security_schemes: frozenset[str]  # aliases


def _scope_registry(reg: Registry, project_module: str | None) -> _Scope:
    """Compute the inclusion set for a desired snapshot.

    Unscoped (``project_module=None``): the whole registry, the historical
    behavior. Scoped: only the ``Project`` classes defined in
    ``project_module`` (matched on ``__module__``), plus everything reachable
    from them -- their issuetypes, the custom fields those issuetypes
    declare, and the screens/screen schemes/field configurations referenced
    along the way. Importing a sibling schema module (the documented reuse
    pattern) must not leak its projects into an autogenerated migration.

    Declared-but-not-yet-wired objects are also included when their
    declaring module matches ``project_module`` (M5): a Screen/CustomField
    declared in the schema but not referenced by any scheme must not be
    scoped out, or it lands in state as an "orphan" diff reports forever
    (and deletes under --allow-delete). Declaring module is recorded by the
    registry at registration time (``_declared_module``)."""
    if project_module is None:
        return _Scope(
            projects=dict(reg.projects),
            issuetypes=frozenset(reg.issuetypes),
            custom_fields=frozenset(reg.custom_fields),
            screens=frozenset(reg.screens),
            screen_schemes=frozenset(reg.screen_schemes),
            field_configurations=frozenset(reg.field_configurations),
            security_schemes=frozenset(reg.security_schemes),
        )
    projects = {k: cls for k, cls in reg.projects.items() if cls.__module__ == project_module}
    it_classes = [it for cls in projects.values() for it in getattr(cls, "__issuetypes__", [])]
    field_aliases = {cf.alias for it in it_classes for cf in it.__custom_field_map__.values()}
    # ScreenScheme / FieldConfiguration are mutable dataclasses (not hashable);
    # ScreenScheme / FieldConfiguration are mutable dataclasses (not hashable);
    # dedupe by id() and resolve aliases once at the end.
    schemes_by_id: dict[int, ScreenScheme] = {}
    for it in it_classes:
        ss: ScreenScheme | None = getattr(it, "__screen_scheme__", None)
        if ss is not None:
            schemes_by_id[id(ss)] = ss
    schemes = list(schemes_by_id.values())
    screens: list[Screen] = []
    for scheme in schemes:
        for s in (scheme.create, scheme.edit, scheme.view):
            if id(s) not in {id(x) for x in screens}:
                screens.append(s)
    field_aliases |= {ref.alias for s in screens for ref in s.fields if isinstance(ref, CustomField)}
    fcs_by_id: dict[int, FieldConfiguration] = {}
    for it in it_classes:
        fc: FieldConfiguration | None = getattr(it, "__field_configuration__", None)
        if fc is not None:
            fcs_by_id[id(fc)] = fc
    fcs = list(fcs_by_id.values())
    field_aliases |= {ref.alias for fc in fcs for ref in (*fc.required, *fc.hidden) if isinstance(ref, CustomField)}

    def _declared_in_module(obj: object) -> bool:
        return getattr(obj, "_declared_module", "") == project_module

    field_aliases |= {a for a, cf in reg.custom_fields.items() if _declared_in_module(cf)}
    # Standalone declarations must reach their CustomFields in turn (a local
    # Screen refers to them), or the screen's refs would not resolve.
    for scr in reg.screens.values():
        if _declared_in_module(scr):
            if scr.alias not in {s.alias for s in screens}:
                screens.append(scr)
            field_aliases |= {ref.alias for ref in scr.fields if isinstance(ref, CustomField)}
    schemes.extend(scheme for scheme in reg.screen_schemes.values() if _declared_in_module(scheme))
    fcs.extend(fc for fc in reg.field_configurations.values() if _declared_in_module(fc))
    # Security schemes reach the scope two ways: referenced by a scoped
    # Project's __security_scheme__, and declared-but-not-yet-wired in the
    # schema module itself (M5 rule, so a declared scheme is diffed instead
    # of silently ignored).
    security_schemes = {
        scheme.alias
        for proj_cls in projects.values()
        if (scheme := getattr(proj_cls, "__security_scheme__", None)) is not None
    }
    security_schemes |= {alias for alias, scheme in reg.security_schemes.items() if _declared_in_module(scheme)}
    return _Scope(
        projects=projects,
        issuetypes=frozenset(getattr(it, "__alias__", "") for it in it_classes),
        custom_fields=frozenset(field_aliases),
        screens=frozenset(s.alias for s in screens),
        screen_schemes=frozenset(scheme.alias for scheme in schemes),
        field_configurations=frozenset(fc.alias for fc in fcs),
        security_schemes=frozenset(security_schemes),
    )


def build_desired_snapshot(
    registry: Registry | None = None,
    *,
    project_module: str | None = None,
) -> DesiredSnapshot:
    """Walk the registry and assemble a complete desired snapshot.

    The default registry is the process-wide one populated by class definition.
    Pass an explicit registry for isolation in tests. Pass ``project_module``
    to scope the snapshot to the Project classes defined in that module (see
    ``_scope_registry``).
    """
    reg = registry or default_registry
    scope = _scope_registry(reg, project_module)
    desired = DesiredSnapshot()

    # ── Custom fields ────────────────────────────────────────────────
    for alias, cf in reg.custom_fields.items():
        if alias not in scope.custom_fields:
            continue
        desired.custom_fields[alias] = DesiredCustomField(
            alias=alias,
            name=cf.name,
            type_id=cf.type.jira_type_id,
            description=cf.description,
            options=tuple(cf.options),
            searcher_key=cf.type.searcher_key,
        )

    # ── Issue types ──────────────────────────────────────────────────
    for alias, it_cls in reg.issuetypes.items():
        if alias not in scope.issuetypes:
            continue
        desired.issuetypes[alias] = DesiredIssueType(
            alias=alias,
            name=getattr(it_cls, "__title__", alias),
            description=getattr(it_cls, "__description__", "") or "",
            subtask=getattr(it_cls, "__subtask__", False),
        )

    # ── Screens (one tab per screen: "Fields") ──────────────────────
    for alias, scr in reg.screens.items():
        if alias not in scope.screens:
            continue
        field_refs: list[str] = []
        for ref in scr.fields:
            if isinstance(ref, CustomField):
                field_refs.append(ref.alias)  # marker: resolve via state at apply time
            else:
                field_refs.append(str(ref))  # system field
        desired.screens[alias] = DesiredScreen(
            alias=alias,
            name=scr.name,
            description=scr.description,
            tabs=(DesiredScreenTab(name="Fields", field_refs=tuple(field_refs)),),
        )

    # ── Screen schemes ───────────────────────────────────────────────
    for alias, ss in reg.screen_schemes.items():
        if alias not in scope.screen_schemes:
            continue
        # Map the user's create/edit/view to Jira's default/create/edit/view.
        # Default falls back to view (matches Jira's "no override" behavior).
        screens = {
            "default": ss.view.alias,
            "create": ss.create.alias,
            "edit": ss.edit.alias,
            "view": ss.view.alias,
        }
        desired.screen_schemes[alias] = DesiredScreenScheme(
            alias=alias,
            name=ss.name,
            description=ss.description,
            screens=screens,
        )

    # ── Field configurations ─────────────────────────────────────────
    for alias, fc in reg.field_configurations.items():
        if alias not in scope.field_configurations:
            continue
        items: list[DesiredFieldConfigurationItem] = []
        for ref in fc.required:
            field_alias = ref.alias if isinstance(ref, CustomField) else str(ref)
            items.append(
                DesiredFieldConfigurationItem(
                    field_alias=field_alias,
                    required=True,
                    hidden=False,
                )
            )
        for ref in fc.hidden:
            field_alias = ref.alias if isinstance(ref, CustomField) else str(ref)
            items.append(
                DesiredFieldConfigurationItem(
                    field_alias=field_alias,
                    required=False,
                    hidden=True,
                )
            )
        desired.field_configurations[alias] = DesiredFieldConfiguration(
            alias=alias,
            name=fc.name,
            description=fc.description,
            items=tuple(items),
        )

    # ── Projects + derived IssueTypeScheme / ITSS / FCS / security ───
    security_scheme_of_project: dict[str, str] = {}
    for project_key, proj_cls in scope.projects.items():
        linked = getattr(proj_cls, "__security_scheme__", None)
        if linked is not None:
            security_scheme_of_project[project_key] = linked.alias
    for project_key, proj_cls in scope.projects.items():
        issuetypes = list(getattr(proj_cls, "__issuetypes__", []))
        its_alias = _derive_its(desired, project_key, issuetypes)
        itss_alias = _derive_itss(desired, project_key, issuetypes)
        fcs_alias = _derive_fcs(desired, project_key, issuetypes)
        desired.projects[project_key] = DesiredProject(
            alias=project_key,
            key=project_key,
            name=getattr(proj_cls, "__title__", project_key),
            project_type_key=getattr(proj_cls, "__project_type__", "software"),
            lead=getattr(proj_cls, "__lead__", "") or "",
            description=getattr(proj_cls, "__description__", "") or "",
            style=getattr(proj_cls, "__style__", "company-managed"),
            issuetype_scheme=its_alias,
            issuetype_screen_scheme=itss_alias,
            field_configuration_scheme=fcs_alias,
            security_scheme=security_scheme_of_project.get(project_key),
        )
    # ── Security schemes ─────────────────────────────────────────────
    for alias, scheme in reg.security_schemes.items():
        if alias not in scope.security_schemes:
            continue
        # Authoritative linkage is Project.__security_scheme__; the scheme's
        # own ``project`` hint is the fallback (the diff warns when no
        # declared Project carries that key, e.g. a renamed project).
        bound_project = next(
            (pk for pk, scheme_alias in security_scheme_of_project.items() if scheme_alias == alias),
            None,
        )
        desired.security_schemes[alias] = DesiredSecurityScheme(
            alias=alias,
            name=scheme.name or alias,
            description=scheme.description or "",
            levels=tuple(
                DesiredSecurityLevel(
                    alias=level.id or level.name,
                    name=level.name,
                    description=level.description or "",
                    is_default=level.is_default or scheme.default_level_designation == (level.id or level.name),
                    members=tuple(DesiredSecurityLevelMember(type=m.type, value=m.name) for m in level.members),
                )
                for level in scheme.levels
            ),
            default_level=scheme.default_level_designation,
            project=bound_project or scheme.project,
        )

    return desired


def _derive_its(
    desired: DesiredSnapshot,
    project_key: str,
    issuetypes: list,
) -> str | None:
    """Synthesize an IssueTypeScheme for the project from its ``__issuetypes__``
    list. None if the project has no issuetypes (the project will fall through
    to Jira's default scheme). Default issuetype is the first non-subtask
    entry (Atlassian requires at least one standard issuetype in a scheme)."""
    if not issuetypes:
        return None
    aliases = [it_cls.__alias__ for it_cls in issuetypes]
    standard = [it_cls.__alias__ for it_cls in issuetypes if not getattr(it_cls, "__subtask__", False)]
    if not standard:
        # Jira rejects schemes with only subtasks; skip derivation and let
        # the project use Jira's default scheme.
        return None
    alias = f"{project_key}_its"
    desired.issuetype_schemes[alias] = DesiredIssueTypeScheme(
        alias=alias,
        name=f"{project_key} Issue Type Scheme",
        description=f"Auto-derived for project {project_key}",
        issuetypes=tuple(aliases),
        default_issuetype=standard[0],
    )
    return alias


def _derive_itss(
    desired: DesiredSnapshot,
    project_key: str,
    issuetypes: list,
) -> str | None:
    """Synthesize an ITSS for the project. None if no issuetype carries a
    screen scheme. Mappings: each issuetype's __screen_scheme__ is its mapping;
    'default' = the first issuetype that carries one."""
    typed: list[tuple[str, str]] = []  # (issuetype_alias, ss_alias)
    for it_cls in issuetypes:
        ss = getattr(it_cls, "__screen_scheme__", None)
        if ss is not None:
            typed.append((it_cls.__alias__, ss.alias))
    if not typed:
        return None
    alias = f"{project_key}_itss"
    mappings: dict[str, str] = {"default": typed[0][1]}
    for it_alias, ss_alias in typed:
        mappings[it_alias] = ss_alias
    desired.issuetype_screen_schemes[alias] = DesiredIssueTypeScreenScheme(
        alias=alias,
        name=f"{project_key} Issue Type Screen Scheme",
        description=f"Auto-derived for project {project_key}",
        mappings=mappings,
    )
    return alias


def _derive_fcs(
    desired: DesiredSnapshot,
    project_key: str,
    issuetypes: list,
) -> str | None:
    """Same shape as _derive_itss but for FieldConfiguration."""
    typed: list[tuple[str, str]] = []
    for it_cls in issuetypes:
        fc = getattr(it_cls, "__field_configuration__", None)
        if fc is not None:
            typed.append((it_cls.__alias__, fc.alias))
    if not typed:
        return None
    alias = f"{project_key}_fcs"
    mappings: dict[str, str] = {"default": typed[0][1]}
    for it_alias, fc_alias in typed:
        mappings[it_alias] = fc_alias
    desired.field_configuration_schemes[alias] = DesiredFieldConfigurationScheme(
        alias=alias,
        name=f"{project_key} Field Configuration Scheme",
        description=f"Auto-derived for project {project_key}",
        mappings=mappings,
    )
    return alias
