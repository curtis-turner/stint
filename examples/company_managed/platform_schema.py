"""Example stint schema for a fictional 'Platform' Jira project.

Demonstrates:

- CustomField declarations with select-type options.
- Screen and ScreenScheme composition, including the restricted-Edit-screen
  pattern: Severity is on create/view but OFF edit, so regular users cannot
  change it in the UI.
- FieldConfiguration declaration.
- IssueType with dual role: schema-plane metadata (__alias__, __display_name__,
  __screen_scheme__) and data-plane Pydantic fields (summary, severity, ...).
- Project with __style__ = "company-managed".
- The automation layer: a ControlFieldGuard (the detect-and-revert rule the
  cosmetic screen lock needs) and an AutomationRule from a UI export, bound
  with __automation_rule_scheme__.

Run `python -m stint.validate` against this module to confirm no internal
inconsistencies. (CLI not implemented in M0 spike; see tests/test_m0_spike.py.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from stint import (
    AutomationRule,
    AutomationRuleScheme,
    ControlFieldGuard,
    CustomField,
    FieldConfiguration,
    Group,
    IssueType,
    Project,
    Screen,
    ScreenScheme,
    SelectField,
    TextField,
)

# ── Custom fields ─────────────────────────────────────────────────────
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

# NOTE: the variable must NOT be named `test_text_field` — that is also the
# model attribute name, and under `from __future__ import annotations` the
# attribute's default (`None`) shadows this declaration when the annotation
# is resolved. Hence the `_cf` suffix.
test_text_field_cf = CustomField(
    alias="test_test_field", name="TestTextField", type=TextField, description="Test adding a new text field to schema"
)

# ── Screens ───────────────────────────────────────────────────────────
# Names match the deployed tenant (the walkthrough's live project).
bug_create_screen = Screen(
    alias="bug_create",
    name="SEC Create Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field, root_cause_field, test_text_field_cf],
)

bug_edit_screen = Screen(
    alias="bug_edit",
    name="SEC Edit Screen",
    # Severity is a control field: OFF the edit screen. Regular users see it
    # on create/view but cannot change it in the UI. The lock is cosmetic for
    # REST clients (JRACLOUD-98054) — the guard below is the enforcement.
    fields=["Summary", "Description", "Reporter", "Assignee", root_cause_field, test_text_field_cf],
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

# ── Screen scheme ─────────────────────────────────────────────────────
bug_screen_scheme = ScreenScheme(
    alias="bug_screens",
    name="SEC Screen Scheme",
    create=bug_create_screen,
    edit=bug_edit_screen,
    view=bug_view_screen,
)

# ── Field configuration ───────────────────────────────────────────────
bug_field_config = FieldConfiguration(
    alias="bug_fields",
    name="SEC Field Configuration",
    required=["Summary", "Reporter", severity_field, test_text_field_cf],
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
    test_text_field: Annotated[str | None, test_text_field_cf] = None


# ── Automation: the control-field guard ──────────────────────────────
# The REST API ignores screens on edit (JRACLOUD-98054), so the screen
# lock above does not stop a determined writer — the guard is the
# enforcement: an Automation rule that reverts edits to the derived
# control fields (create/view minus edit — here, Severity) by anyone
# outside allowed_writers, then comments on the issue.
plat_guard = ControlFieldGuard(
    alias="plat_control_guard",
    name="PLAT: revert control-field edits",
    description="Reverts edits to control fields by non-allowed writers.",
    # No fields=: the control fields derive from the screens.
    allowed_writers=[
        # The account stint authenticates as (STINT_USER) must be exempt, or
        # apply refuses — the guard would otherwise revert the service's own
        # writes. Create the group in Jira and add that account, or swap in
        # User("you@example.com"). ProjectRole("Administrators") works too
        # once the role is declared in a project's __roles__.
        Group("stint-service-accounts"),
    ],
    on_violation=["revert", "comment"],
)

# Escape hatch: a hand-authored rule from the Automation UI's export
# (GET rule/{uuid}), with tenant ids replaced by stint references.
plat_notify = AutomationRule(
    alias="plat_notify_on_close",
    name="PLAT: notify owner on close",
    payload="automation/notify_on_close.json",  # relative to this file
    refs={"severity": severity_field},  # "{{stint.field.severity}}" -> customfield id
    enabled=True,
)

plat_rules = AutomationRuleScheme(alias="plat_rules", name="PLAT Automation", rules=[plat_guard, plat_notify])


# ── Project ───────────────────────────────────────────────────────────
class Platform(Project):
    # The key and title match the walkthrough's deployed project (shared with
    # security_scheme.py's walkthrough — one project carries both; import one
    # schema module per process, the registry rejects the duplicate).
    __key__ = "SEC"
    __title__ = "SecurityPlatform"
    __lead__ = "cturner@example.com"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
    __automation_rule_scheme__ = plat_rules  # every rule scopes to the bound projects
