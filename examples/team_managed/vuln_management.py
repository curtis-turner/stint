"""Example stint schema for a fictional 'Vulnerability Management' team-managed
Jira Cloud project.

Demonstrates the team-managed (TMP) path, which is a separate dialect from
the company-managed schema in platform.py:

- CustomField declarations work the same as CMP (select + text fields).
- IssueType with plain data-plane fields only -- no __screen_scheme__ or
  __field_configuration__. Team-managed projects don't have reusable
  screens/schemes; ProjectMeta rejects a team-managed Project whose
  issuetypes reference either.
- Project with __style__ = "team-managed".
- A ControlFieldGuard bound with __automation_rule_scheme__: the
  detect-and-revert rule that revokes out-of-band writes to the control
  field. Two team-managed requirements (enforced by ProjectMeta): the
  guard's fields are explicit (no screens to derive control fields from)
  and its allowed_writers are users/groups (team-managed projects have no
  classic project roles). The rules themselves are tenant objects scoped
  by project id -- identical wire behavior on both project styles.

Reflecting and applying this schema uses stint.dialects.jira.tmp's
experimental, opt-in jira_cloud_tmp dialect -- a different CLI path from
platform.py's migration-based one (`stint reflect --dialect jira_cloud_tmp`
and `stint apply`, not `stint revision`/`stint upgrade`). See
examples/README.md's "Team-managed (TMP)" walkthrough.
"""

from __future__ import annotations

from typing import Annotated, Literal

from stint import (
    AutomationRuleScheme,
    ControlFieldGuard,
    CustomField,
    IssueType,
    Project,
    SelectField,
    TextField,
    User,
)

severity_field = CustomField(
    alias="vuln_severity",
    name="Severity",
    type=SelectField,
    description="Vulnerability severity classification",
    options=["S1", "S2", "S3", "S4"],
)

root_cause_field = CustomField(
    alias="vuln_root_cause",
    name="Root Cause",
    type=TextField,
)


# ── Issue type ────────────────────────────────────────────────────────
class Vulnerability(IssueType):
    __alias__ = "vulnerability"
    __title__ = "Vulnerability"
    __description__ = "A security vulnerability tracked for remediation"

    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]
    root_cause: Annotated[str, root_cause_field] = ""


# ── The control-field guard ───────────────────────────────────────────
# Team-managed projects have no field-level lock at all: anyone who can
# edit an issue can edit every editable field, in the UI and over the REST
# API alike. The guard is the enforcement: an Automation rule fires on
# "Field value changed" for the declared control fields and puts the
# previous value back when the writer is not in allowed_writers.
vuln_guard = ControlFieldGuard(
    alias="vuln_control_guard",
    name="VM: revert control-field edits",
    description="Reverts edits to control fields by non-allowed writers.",
    fields=[severity_field],  # explicit: TMP has no screens to derive from
    allowed_writers=[
        # The account stint authenticates as (STINT_USER below) must be here,
        # or apply refuses: the guard would otherwise revert the service's
        # own writes. Replace with your own account email.
        User("svc-automation@example.com"),
        # Group("vuln-managers"),  # groups work; project roles do not (TMP has none)
    ],
    on_violation=["revert", "comment"],
)

vuln_rules = AutomationRuleScheme(
    alias="vuln_rules",
    name="VM Automation",
    rules=[vuln_guard],
)


# ── Project ───────────────────────────────────────────────────────────
class VulnManagement(Project):
    __key__ = "VM"
    __style__ = "team-managed"
    __issuetypes__ = [Vulnerability]
    __automation_rule_scheme__ = vuln_rules  # rules scope to the bound projects
