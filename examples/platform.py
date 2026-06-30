"""Example stint schema for a fictional 'Platform' Jira project.

Demonstrates:

- CustomField declarations with select-type options.
- Screen and ScreenScheme composition.
- FieldConfiguration declaration.
- IssueType with dual role: schema-plane metadata (__alias__, __display_name__,
  __screen_scheme__) and data-plane Pydantic fields (summary, severity, ...).
- Project with __style__ = "company-managed".

Run `python -m stint.validate` against this module to confirm no internal
inconsistencies. (CLI not implemented in M0 spike; see tests/test_m0_spike.py.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from stint import (
    CustomField,
    FieldConfiguration,
    IssueType,
    Project,
    Screen,
    ScreenScheme,
    SelectField,
    TextField,
)

# ── Custom fields ─────────────────────────────────────────────────────
# Convention: name the module-level CustomField with a `_field` suffix so it
# does not collide with the class attribute name used in Annotated[]. Without
# the suffix, a class attribute with a default value would shadow the
# module-level binding during type-hint resolution.
severity_field = CustomField(
    alias="bug_severity",
    name="Severity",
    type=SelectField,
    description="Bug severity classification",
    options=["S1", "S2", "S3", "S4"],
)

root_cause_field = CustomField(
    alias="bug_root_cause",
    name="Root Cause",
    type=TextField,
)

# ── Screens ───────────────────────────────────────────────────────────
bug_create_screen = Screen(
    alias="bug_create",
    name="Bug Create Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field],
)

bug_edit_screen = Screen(
    alias="bug_edit",
    name="Bug Edit Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field, root_cause_field],
)

bug_view_screen = Screen(
    alias="bug_view",
    name="Bug View Screen",
    fields=[
        "Summary",
        "Description",
        "Reporter",
        "Assignee",
        severity_field,
        root_cause_field,
        "Created",
        "Updated",
    ],
)

# ── Screen scheme ─────────────────────────────────────────────────────
bug_screen_scheme = ScreenScheme(
    alias="bug_screens",
    name="Bug Screen Scheme",
    create=bug_create_screen,
    edit=bug_edit_screen,
    view=bug_view_screen,
)

# ── Field configuration ───────────────────────────────────────────────
bug_field_config = FieldConfiguration(
    alias="bug_fields",
    name="Bug Field Configuration",
    required=["Summary", "Reporter", severity_field],
)


# ── Issue type ────────────────────────────────────────────────────────
class Bug(IssueType):
    __alias__ = "bug"
    __description__ = "Defects in production"
    __screen_scheme__ = bug_screen_scheme
    __field_configuration__ = bug_field_config

    # Pydantic-typed fields. System fields are bare annotations. Custom fields
    # use Annotated[T, custom_field_obj] so the schema plane can find them.
    summary: str
    description: str | None = None
    reporter: str
    assignee: str | None = None
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]
    root_cause: Annotated[str | None, root_cause_field] = None
    created: datetime | None = None
    updated: datetime | None = None


# ── Project ───────────────────────────────────────────────────────────
class Platform(Project):
    __key__ = "PLAT"
    __lead__ = "cturner@example.com"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
