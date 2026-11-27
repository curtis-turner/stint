"""Desired-state builder for one team-managed project.

Deliberately separate from ``stint.autogen.desired.build_desired_snapshot``:
that builder derives CMP-only scheme objects (issue-type schemes, issue-type
screen schemes, field-configuration schemes) that team-managed projects
don't have and are forbidden from referencing (see ``ProjectMeta``). Reuses
the *same* schema classes (``Project``, ``IssueType``, ``CustomField``) --
there is no separate TMP schema DSL -- just a different, simpler read of them.

Field-type validation: only 9 of the 18 ``_FieldType`` classes in
``stint.fields`` are accepted by the TMP ``createCustomFieldInProjectAndAdd
ToAllIssueTypes`` mutation. ``tmp_type_key_for`` enforces that at plan
time -- a schema declaring an unsupported type fails loudly here, not as
a raw 400 from a live create call. The supported set lives in
``stint/dialects/jira/tmp/types.py``; re-verify it with
``tools/tmp_type_probe.py``.

Known gap: there is no schema-level way to mark a field required on a TMP
work type today (``FieldConfiguration.required`` is CMP-only and forbidden
for team-managed projects by ``ProjectMeta``). TMP fields are therefore
always planned as not-required. Extending schema to cover this is a core
change outside this package's isolation boundary, deferred to a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stint.autogen.desired import DesiredAutomationRule, automation_rule_record
from stint.dialects.jira.tmp.types import tmp_type_key_for
from stint.fields import CustomField
from stint.registry import registry as default_registry
from stint.schema.project import Project


@dataclass(frozen=True)
class TmpDesiredField:
    """One project-scoped custom field, as declared in schema."""

    alias: str
    name: str
    type_key: str
    description: str = ""
    options: tuple[str, ...] = ()  # option values (not ids -- those are Jira's to assign)


@dataclass(frozen=True)
class TmpDesiredWorkType:
    """One work type (issue type), as declared in schema."""

    alias: str
    name: str
    description: str = ""
    field_aliases: tuple[str, ...] = ()  # in declared (Annotated) attribute order


@dataclass(frozen=True)
class TmpDesired:
    """Everything declared for one team-managed project."""

    project_key: str
    fields: dict[str, TmpDesiredField] = field(default_factory=dict)
    worktypes: dict[str, TmpDesiredWorkType] = field(default_factory=dict)
    # The bound AutomationRuleScheme's rules (tenant objects; scope ARIs
    # point at this project). Guards declare their fields explicitly —
    # team-managed projects have no screens to derive control fields from
    # (enforced by ProjectMeta validation).
    automation_rules: dict[str, DesiredAutomationRule] = field(default_factory=dict)


def _wired_field_aliases() -> set[str]:
    """Aliases referenced by *any* schema wiring, in any project.

    A CustomField referenced by an IssueType, Screen, or FieldConfiguration
    belongs to that declaration's project (often company-managed, possibly
    in an imported sibling module). Module-level unwired declarations are
    the only ones this builder may claim for a team-managed project.
    """
    wired: set[str] = set()
    for it_cls in default_registry.issuetypes.values():
        wired.update(cf.alias for cf in it_cls.__custom_field_map__.values())
    for scr in default_registry.screens.values():
        wired.update(ref.alias for ref in scr.fields if isinstance(ref, CustomField))
    for fc in default_registry.field_configurations.values():
        wired.update(ref.alias for ref in (*fc.required, *fc.hidden) if isinstance(ref, CustomField))
    return wired


def build_tmp_desired(project_cls: type[Project]) -> TmpDesired:
    """Read one ``Project`` subclass's ``__issuetypes__`` into a ``TmpDesired``.

    Does not check ``__style__`` -- callers already know they're building the
    TMP path (mirroring ``build_desired_snapshot``, which likewise takes
    project classes as given rather than re-validating them).

    Module-level ``CustomField``s declared in the project's module that no
    schema wiring references are included too: a project-scoped field that
    exists in Jira but sits on no work-type layout can only be adopted (or
    deleted) once the desired state carries it. Their layout membership
    stays unmanaged -- nothing adds them to a work type. The declaring
    module match mirrors CMP ``_scope_registry`` (M5); wiring-referenced
    fields are excluded so a mixed module's CMP-facing declarations are not
    created in the team-managed project.
    """
    fields: dict[str, TmpDesiredField] = {}
    worktypes: dict[str, TmpDesiredWorkType] = {}
    for it in getattr(project_cls, "__issuetypes__", []):
        field_aliases = []
        for cf in it.__custom_field_map__.values():
            field_aliases.append(cf.alias)
            fields[cf.alias] = TmpDesiredField(
                alias=cf.alias,
                name=cf.name,
                type_key=tmp_type_key_for(cf.type),
                description=cf.description,
                options=tuple(cf.options),
            )
        worktypes[it.__alias__] = TmpDesiredWorkType(
            alias=it.__alias__,
            name=it.__title__,
            description=getattr(it, "__description__", None) or "",
            field_aliases=tuple(field_aliases),
        )
    project_module = getattr(project_cls, "_declared_module", "") or project_cls.__module__
    wired = _wired_field_aliases()
    for alias, cf in default_registry.custom_fields.items():
        if alias in fields or alias in wired:
            continue
        if getattr(cf, "_declared_module", "") != project_module:
            continue
        fields[alias] = TmpDesiredField(
            alias=cf.alias,
            name=cf.name,
            type_key=tmp_type_key_for(cf.type),
            description=cf.description,
            options=tuple(cf.options),
        )
    project_key = getattr(project_cls, "__key__", "")
    automation_rules: dict[str, DesiredAutomationRule] = {}
    scheme = getattr(project_cls, "__automation_rule_scheme__", None)
    if scheme is not None:
        for rule in scheme.rules:
            automation_rules[rule.alias] = automation_rule_record(rule, bound_projects=(project_key,), proj_cls=None)
    return TmpDesired(project_key=project_key, fields=fields, worktypes=worktypes, automation_rules=automation_rules)
