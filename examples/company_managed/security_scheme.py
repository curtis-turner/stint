"""Example stint schema for a fictional 'Security' Jira project.

Demonstrates issue security scheme integration with company-managed projects:

- CustomField declarations with select-type options.
- SecurityLevel and SecurityLevelMember declarations.
- SecurityScheme declaration attached to the Project.
- IssueType with data-plane fields and security scheme annotations.
- Project with __style__ = "company-managed" and __security_scheme__.

Run `python -m stint validate` against this module to confirm no internal
inconsistencies. (CLI not implemented in M0 spike; see tests/ for integration.)

The example uses the ``jira_cloud`` dialect (company-managed projects). For
team-managed security, see the TMP dialect in ``examples/team_managed/``.
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
    SecurityLevel,
    SecurityLevelMember,
    SecurityScheme,
    SelectField,
    TextField,
)

# ── Custom fields ───────────────────────────────────────────────────────
# Convention: give the module-level CustomField a distinguishing suffix
# (`_field`, `_cf`) so it does not collide with the class attribute name used
# in Annotated[]. Without a suffix, a class attribute with a default value
# would shadow the module-level binding during type-hint resolution.
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

environment_field = CustomField(
    alias="bug_environment",
    name="Environment",
    type=TextField,
    description="Where the bug was found (e.g. production, staging)",
)

# NOTE: the variable must NOT be named `test_text_field` — that is also the
# model attribute name, and under `from __future__ import annotations` the
# attribute's default (`None`) shadows this declaration when the annotation
# is resolved. Hence the `_cf` suffix.
test_text_field_cf = CustomField(
    alias="test_test_field", name="TestTextField", type=TextField, description="Test adding a new text field to schema"
)

# ── Security levels ─────────────────────────────────────────────────────
# Define security levels that will be part of a security scheme.
# These are declared at the schema level so stint knows about them during
# reflect/diff/apply cycles.
#
# A security level only grants visibility to its MEMBERS. Jira has no
# "everyone" member, so a level with no members (including one named
# "Public") hides its issues from everyone without the project's "Set issue
# security" permission. Worse, if such a level is the scheme's DEFAULT,
# every issue the data plane creates is invisible: JQL returns nothing and
# GET/PUT /issue/{key} answer 404, even for the token that just created it.
#
# Every level below carries a member, so none is a "dead" level: if one of
# them is ever made the default, its issues stay visible. (Jira assigns no
# security level when a scheme has no default, which also means "visible to
# anyone who can browse the project" — we keep that below.)

# ── Security level members ──────────────────────────────────────────────
# ``type="user"`` takes an email or raw accountId; emails are resolved at
# apply time. ``type="group"`` takes a group name. The project lead is the
# one account guaranteed to exist on this tenant, so the example uses it;
# a real scheme would use groups or project roles for the intended audience.
lead_member = SecurityLevelMember(
    id="user-project-lead",
    type="user",
    name="cjturner714@gmail.com",
)

public_level = SecurityLevel(
    id="level-public",
    name="Public",
    description="Anyone can see issues with this level",
    members=[lead_member],
)

internal_level = SecurityLevel(
    id="level-internal",
    name="Internal",
    description="Company employees can see these issues",
    members=[lead_member],
)

reporter_level = SecurityLevel(
    id="level-reporter",
    name="Reporter Only",
    description="Only the reporter and internal staff can see this issue",
    members=[lead_member],
)

# ── Security scheme ─────────────────────────────────────────────────────
# A SecurityScheme ties together security levels and optionally default-level
# configuration. The ``project`` reference links this scheme to the Project
# so that ``stint`` knows which scheme to associate during ``apply``.
# ``default_level=None`` clears any default left on the live scheme so new
# issues stay visible (see the note above).

bug_security_scheme = SecurityScheme(
    alias="bug_security",
    name="Bug Security Scheme",
    description="Controls who can see bug-related issues",
    levels=[
        public_level,
        internal_level,
        reporter_level,
    ],
    project="SEC",  # Links to the Project below (must match its __key__)
)


# ── Screens ─────────────────────────────────────────────────────────────
# Issuetypes need screens or Jira's default create/edit screens for the
# project don't show the custom fields — and the data plane (issue creation)
# 400s with "Field 'X' cannot be set. It is not on the appropriate screen".
# Without these declarations the ones the demo's inserts set are invisible.
#
# Names must be unique tenant-wide (Jira enforces it), and Jira already
# created default screens named "Bug Create/Edit/View Screen" when the SEC
# project adopted its template — hence the SEC- prefix below.
#
# Reporter stays on the create/edit/view screens (it is a normal screen
# field for reassignment), but it is NOT set in create payloads: Jira fills
# reporter from the caller's identity on create.
bug_create_screen = Screen(
    alias="bug_create",
    name="SEC Create Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field, test_text_field_cf],
)

bug_edit_screen = Screen(
    alias="bug_edit",
    name="SEC Edit Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field, root_cause_field, test_text_field_cf],
)

bug_view_screen = Screen(
    alias="bug_view",
    name="SEC View Screen",
    fields=[
        "Summary",
        "Description",
        "Reporter",
        "Assignee",
        severity_field,
        root_cause_field,
        test_text_field_cf,
        "Created",
        "Updated",
    ],
)

# ── Screen scheme ────────────────────────────────────────────────────────
bug_screen_scheme = ScreenScheme(
    alias="bug_screens",
    name="SEC Screen Scheme",
    create=bug_create_screen,
    edit=bug_edit_screen,
    view=bug_view_screen,
)

# ── Field configuration ──────────────────────────────────────────────
# Necessary on Field Schemes (v2) tenants: creating a custom field makes it
# exist tenant-wide, but until it is ASSOCIATED to the project's field
# scheme the work-item layout warns "This field is not associated to the
# 'Bug' work type" and issue creation rejects it ("Field 'customfield_X'
# cannot be set. It is not on the appropriate screen, or unknown.").
# The `required=[...]` items are what drive the scheme association on apply
# (other tenant fields are associated best-effort at scheme creation) — and
# required custom fields are ENFORCED by Jira at issue creation, so keep this
# list to what a legitimate Bug genuinely must carry.
#
# Names must be unique tenant-wide — prefix with the project key.
bug_field_config = FieldConfiguration(
    alias="bug_fields",
    name="SEC Field Configuration",
    required=["Summary", severity_field],
)


# ── Issue type ──────────────────────────────────────────────────────────
class Bug(IssueType):
    __alias__ = "bug"
    __description__ = "Defects in production"
    __screen_scheme__ = bug_screen_scheme
    __security_scheme__ = bug_security_scheme
    __field_configuration__ = bug_field_config

    # Pydantic-typed fields. System fields are bare annotations. Custom fields
    # use Annotated[T, custom_field_obj] so the schema plane can find them.
    #
    # reporter is optional on CREATE: Jira sets it from the caller's identity;
    # passing an explicit reporter in a create payload 400s ("not on the
    # appropriate screen" — Jira rejects caller-set reporters there even when
    # the field is on the create screen). Hydration still fills reporter
    # (as an accountId) when reading an issue back.
    summary: str
    description: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]
    root_cause: Annotated[str | None, root_cause_field] = None
    environment: Annotated[str | None, environment_field] = None
    created: datetime | None = None
    updated: datetime | None = None
    test_text_field: Annotated[str | None, test_text_field_cf] = None


# ── Project ─────────────────────────────────────────────────────────────
class SecurityPlatform(Project):
    __key__ = "SEC"
    __lead__ = "cjturner714@gmail.com"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
    __security_scheme__ = bug_security_scheme

    # Optional field configuration for the project
    # field_configuration is optional and omitted
