"""Desired-state builder for one team-managed project.

Deliberately separate from ``stint.autogen.desired.build_desired_snapshot``:
that builder derives CMP-only scheme objects (issue-type schemes, issue-type
screen schemes, field-configuration schemes) that team-managed projects
don't have and are forbidden from referencing (see ``ProjectMeta``). Reuses
the *same* schema classes (``Project``, ``IssueType``, ``CustomField``) --
there is no separate TMP schema DSL -- just a different, simpler read of them.

Known gap: there is no schema-level way to mark a field required on a TMP
work type today (``FieldConfiguration.required`` is CMP-only and forbidden
for team-managed projects by ``ProjectMeta``). TMP fields are therefore
always planned as not-required. Extending schema to cover this is a core
change outside this package's isolation boundary, deferred to a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


def build_tmp_desired(project_cls: type[Project]) -> TmpDesired:
    """Read one ``Project`` subclass's ``__issuetypes__`` into a ``TmpDesired``.

    Does not check ``__style__`` -- callers already know they're building the
    TMP path (mirroring ``build_desired_snapshot``, which likewise takes
    project classes as given rather than re-validating them).
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
                type_key=cf.type.jira_type_id,
                description=cf.description,
                options=tuple(cf.options),
            )
        worktypes[it.__alias__] = TmpDesiredWorkType(
            alias=it.__alias__,
            name=it.__title__,
            description=getattr(it, "__description__", None) or "",
            field_aliases=tuple(field_aliases),
        )
    project_key = getattr(project_cls, "__key__", "")
    return TmpDesired(project_key=project_key, fields=fields, worktypes=worktypes)
