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
    IssueType,
    Project,
    SecurityLevel,
    SecurityLevelMember,
    SecurityScheme,
    SelectField,
    TextField,
)

# ── Custom fields ───────────────────────────────────────────────────────
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

environment_field = CustomField(
    alias="bug_environment",
    name="Environment",
    type=TextField,
    description="Where the bug was found (e.g. production, staging)",
)

test_text_field = CustomField(
    alias="test_test_field", name="TestTextField", type=TextField, description="Test adding a new text field to schema"
)

# ── Security levels ─────────────────────────────────────────────────────
# Define security levels that will be part of a security scheme.
# These are declared at the schema level so stint knows about them during
# reflect/diff/apply cycles.

public_level = SecurityLevel(
    id="level-public",
    name="Public",
    description="Anyone can see issues with this level",
    is_default=True,
)

internal_level = SecurityLevel(
    id="level-internal",
    name="Internal",
    description="Company employees can see these issues",
)

reporter_level = SecurityLevel(
    id="level-reporter",
    name="Reporter Only",
    description="Only the reporter and internal staff can see this issue",
)

# ── Security level members ──────────────────────────────────────────────
# Members are assigned to security levels. In a real Jira tenant these would
# be user accounts or groups. Here we demonstrate the schema declaration; the
# actual accountId lookups happen at runtime against your Jira instance.

jira_admins_member = SecurityLevelMember(
    id="group-jira-admins",
    type="group",
    name="jira-admins",
    # In practice the `id` would be the Jira group ID or accountId,
    # e.g. "admins", "jira-developers", etc.
)

dev_team_member = SecurityLevelMember(
    id="user-example-comm",
    type="user",
    name="user@example.com",
    # In practice: accountId like "5b10ac8b86e24b0016d46e0f"
)

security_ops_member = SecurityLevelMember(
    id="group-security-ops",
    type="group",
    name="security-ops",
)

# ── Security scheme ─────────────────────────────────────────────────────
# A SecurityScheme ties together security levels and optionally default-level
# configuration. The ``project`` reference links this scheme to the Project
# so that ``stint`` knows which scheme to associate during ``apply``.

bug_security_scheme = SecurityScheme(
    alias="bug_security",
    name="Bug Security Scheme",
    description="Controls who can see bug-related issues",
    levels=[
        public_level,
        internal_level,
        reporter_level,
    ],
    project="security-platform",  # Links to the Project below
    default_level="public",  # New issues default to Public visibility
)


# ── Issue type ──────────────────────────────────────────────────────────
class Bug(IssueType):
    __alias__ = "bug"
    __description__ = "Defects in production"
    __security_scheme__ = bug_security_scheme

    # Pydantic-typed fields. System fields are bare annotations. Custom fields
    # use Annotated[T, custom_field_obj] so the schema plane can find them.
    summary: str
    description: str | None = None
    reporter: str
    assignee: str | None = None
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]
    root_cause: Annotated[str | None, root_cause_field] = None
    environment: Annotated[str | None, environment_field] = None
    created: datetime | None = None
    updated: datetime | None = None
    test_text_field: str | None = None


# ── Project ─────────────────────────────────────────────────────────────
class SecurityPlatform(Project):
    __key__ = "SEC"
    __lead__ = "devops@example.com"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
    __security_scheme__ = bug_security_scheme

    # Optional field configuration for the project
    # field_configuration is optional and omitted
