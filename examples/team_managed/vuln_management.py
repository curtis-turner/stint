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

Reflecting and applying this schema uses stint.dialects.jira.tmp's
experimental, opt-in jira_cloud_tmp dialect -- a different CLI path from
platform.py's migration-based one (`stint reflect --dialect jira_cloud_tmp`
and `stint apply`, not `stint revision`/`stint upgrade`). See
examples/README.md's "Team-managed (TMP)" walkthrough.
"""

from __future__ import annotations

from typing import Annotated, Literal

from stint import CustomField, IssueType, Project, SelectField, TextField

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


# ── Project ───────────────────────────────────────────────────────────
class VulnManagement(Project):
    __key__ = "VM"
    __style__ = "team-managed"
    __issuetypes__ = [Vulnerability]
