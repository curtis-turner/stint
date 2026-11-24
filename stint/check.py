"""Runtime schema check: a read-only drift report a long-running service
can call at startup.

``engine.check(Project, state)`` reflects the tenant (one reflect's worth of
reads, no writes) plus the create-metadata endpoint, then compares what the
code declares against what Jira actually has. It answers the question
`stint validate` cannot: does the project STILL match, right now — before
the first insert 400s mid-sync because someone edited the project in the UI.

Findings split by severity:

- **errors** — the declared schema cannot work against this tenant as-is:
  a declared custom field or option is missing, an issuetype is gone or not
  on the project, a field is not on the create screen.
- **warnings** — drift worth knowing about but not fatal: extra options on
  the tenant, renamed issuetype/field descriptions.

``report.raise_for_errors()`` turns errors into a ConfigurationError; the
CLI (`stint check`) exits non-zero on the same condition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stint.exceptions import ConfigurationError
from stint.fields import (
    CheckboxesField,
    CustomField,
    MultiSelectField,
    RadioButtonsField,
    SelectField,
)

if TYPE_CHECKING:
    from stint.engine import Engine
    from stint.schema.issuetype import IssueType
    from stint.schema.project import Project
    from stint.state.file import StateFile

_SELECT_STYLES = (SelectField, MultiSelectField, RadioButtonsField, CheckboxesField)

# Attributes never expected on a create screen: derived/read-only, set
# through transitions, or stripped by stint's own insert builder.
_CREATE_EXEMPT = frozenset({"key", "status", "status_category", "created", "updated", "resolution", "reporter"})


@dataclass
class CheckReport:
    """Findings for one declared project. ``errors`` mean the declared
    schema cannot work against the live tenant; ``warnings`` do not block."""

    project: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            details = "\n".join(f"  - {e}" for e in self.errors)
            raise ConfigurationError(
                f"schema check failed for project {self.project!r} ({len(self.errors)} error(s)):\n{details}"
            )


async def check_project(engine: Engine, project_cls: type[Project], state: StateFile) -> CheckReport:
    """Reflect the tenant and compare the declared project against it.

    Read-only: one full reflect plus one createmeta GET. No state is
    written, nothing is applied.
    """
    report = CheckReport(project=project_cls.__key__)
    dialect = engine.dialect
    snapshot = await dialect.reflect()

    project_key = project_cls.__key__
    proj_snap = snapshot.projects.get(project_key)
    if proj_snap is None:
        report.errors.append(f"project {project_key!r} does not exist on the tenant")
    scheme_members: set[str] | None = None
    if proj_snap is not None and proj_snap.issuetype_scheme_id:
        scheme = snapshot.issuetype_schemes.get(proj_snap.issuetype_scheme_id)
        if scheme is not None:
            scheme_members = set(scheme.issuetype_ids)

    create_fields = await _createmeta_fields(dialect, project_key)

    issuetypes: list[type[IssueType]] = list(getattr(project_cls, "__issuetypes__", ()) or ())
    for it in issuetypes:
        _check_issuetype(it, state, snapshot, report, scheme_members, create_fields)
    return report


def _check_issuetype(
    it: type[IssueType],
    state: Any,
    snapshot: Any,
    report: CheckReport,
    scheme_members: set[str] | None,
    create_fields: dict[str, set[str]],
) -> None:
    """Compare one declared issuetype (and its fields) against the snapshot."""
    label = it.__name__
    alias = getattr(it, "__alias__", "")
    it_mapping = state.issuetypes.get(alias)
    if it_mapping is None:
        report.errors.append(
            f"{label}: issuetype {alias!r} is not in state; run `stint stamp` or `stint upgrade` first."
        )
        return
    it_snap = snapshot.issuetypes.get(it_mapping.id)
    if it_snap is None:
        report.errors.append(f"{label}: issuetype {alias!r} (id {it_mapping.id}) does not exist on the tenant")
    elif it_snap.name != it.__title__:
        report.warnings.append(
            f"{label}: issuetype renamed on the tenant: declared {it.__title__!r}, live {it_snap.name!r}"
        )
    if scheme_members is not None and it_mapping.id not in scheme_members:
        report.errors.append(
            f"{label}: issuetype {alias!r} (id {it_mapping.id}) is not on project {report.project}'s issue-type scheme"
        )

    meta_fields = create_fields.get(it_mapping.id)
    if meta_fields is None:
        report.errors.append(f"{label}: no create screen for this issuetype on project {report.project}")
        return

    for attr_name, field_info in it.model_fields.items():
        if attr_name == "key" or attr_name in _CREATE_EXEMPT:
            continue
        cf = next((m for m in field_info.metadata if isinstance(m, CustomField)), None)
        if cf is None:
            if attr_name not in meta_fields:
                report.errors.append(
                    f"{label}.{attr_name}: system field {attr_name!r} is not on the create screen for this issuetype"
                )
            continue

        mapping = state.custom_fields.get(cf.alias)
        if mapping is None:
            report.errors.append(
                f"{label}.{attr_name}: custom field {cf.alias!r} is not in state; "
                f"run `stint stamp` or `stint upgrade` first."
            )
            continue
        cf_snap = snapshot.custom_fields.get(mapping.id)
        if cf_snap is None:
            report.errors.append(
                f"{label}.{attr_name}: custom field {cf.alias!r} ({mapping.id}) does not exist on the tenant"
            )
            continue
        if cf.type in _SELECT_STYLES:
            declared = list(cf.options or [])
            live = set(cf_snap.options)
            missing = [o for o in declared if o not in live]
            if missing:
                report.errors.append(
                    f"{label}.{attr_name}: option(s) {missing} missing from field "
                    f"{cf.name!r} ({mapping.id}); live options: {sorted(live)}"
                )
            extra = sorted(live - set(declared))
            if extra:
                report.warnings.append(
                    f"{label}.{attr_name}: field {cf.name!r} has option(s) not declared in the schema: {extra}"
                )
        if cf.description and cf_snap.description and cf_snap.description != cf.description:
            report.warnings.append(f"{label}.{attr_name}: field {cf.name!r} description changed on the tenant")
        if mapping.id not in meta_fields:
            report.errors.append(
                f"{label}.{attr_name}: field {cf.alias!r} ({mapping.id}) is not on the create screen for this issuetype"
            )


async def _createmeta_fields(dialect: Any, project_key: str) -> dict[str, set[str]]:
    """issuetype id -> set of field keys settable on create.

    Read-only: one ``GET /issue/createmeta?projectKeys=...&expand=projects.issuetypes.fields``.
    """
    raw = await dialect.get_createmeta(project_key)
    out: dict[str, set[str]] = {}
    if not isinstance(raw, dict):
        return out
    for proj in raw.get("projects") or []:
        for it in proj.get("issuetypes") or []:
            fields = it.get("fields") or {}
            out[str(it.get("id", ""))] = set(fields.keys())
    return out
