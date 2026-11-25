"""Example stint schema: automation rules and the control-field guard.

Companion to ``permission_scheme.py`` and ``workflow.py`` (same project
shape). The restricted-Edit-screen pattern locks the Severity control
field in the UI only — the REST API still accepts writes from anyone
holding ``EDIT_ISSUES`` (JRACLOUD-98054). This schema declares the
enforcement layer that actually works:

- ``ControlFieldGuard`` generates the detect-and-revert automation rule:
  trigger on Field value changed for the derived control fields, revert
  edits by anyone outside ``allowed_writers``, comment on the issue.
- ``AutomationRule`` is the escape hatch: a rule exported from the
  Automation UI with tenant ids replaced by ``{{stint.*}}`` references
  (``refs`` maps the names to declared objects; stint substitutes
  customfield ids, accountIds, and role/group ids at apply time).
- ``AutomationRuleScheme`` groups the rules; ``Project
  __automation_rule_scheme__`` scopes every rule in it to the bound
  projects (one multi-project scope, never copies).

The guard's allowed_writers always include the service account — stint
refuses to apply a guard that would revert the service's own writes.

Run `stint validate --schema examples.company_managed.automation` (no
network) to confirm internal consistency.
"""

from __future__ import annotations

from pathlib import Path

from stint import (
    AutomationRule,
    AutomationRuleScheme,
    ControlFieldGuard,
    CustomField,
    IssueType,
    Project,
    ProjectRole,
    Screen,
    ScreenScheme,
    SelectField,
    User,
)

# ── Fields and screens: the control field ────────────────────────────
severity = CustomField(
    alias="severity",
    name="Severity",
    type=SelectField,
    options=["Low", "Medium", "High", "Critical"],
)

create_screen = Screen(alias="tracker_create", name="Tracker Create", fields=["Summary", severity])
# The control field is OFF the edit screen: regular users see it, cannot
# change it. That lock is cosmetic for API clients — the guard below
# reverts out-of-band writes.
edit_screen = Screen(alias="tracker_edit", name="Tracker Edit", fields=["Summary"])
view_screen = Screen(alias="tracker_view", name="Tracker View", fields=["Summary", severity])

screens = ScreenScheme(
    alias="tracker_screens",
    name="Tracker Screens",
    create=create_screen,
    edit=edit_screen,
    view=view_screen,
)


class Bug(IssueType):
    __alias__ = "bug"
    __screen_scheme__ = screens


# ── The generated guard rule ─────────────────────────────────────────
trk_guard = ControlFieldGuard(
    alias="trk_control_guard",
    name="TRK: revert control-field edits",
    description="Reverts edits to control fields by non-allowed writers.",
    # No fields=: the control fields derive from the bound project's
    # screens (create/view minus edit — here, Severity). Pass fields=[]
    # entries (CustomField instances or system field names) to add or
    # narrow.
    allowed_writers=[
        User("svc-automation@example.com"),  # the service account — required
        ProjectRole("Administrators"),
    ],
    on_violation=["revert", "comment"],
)

# ── Escape hatch: a hand-authored rule from the UI export ────────────
# automation/notify_on_close.json is a GET rule/{uuid} export with tenant
# ids replaced: "{{stint.field.severity}}" where the customfield id was.
notify_on_close = AutomationRule(
    alias="trk_notify_on_close",
    name="TRK: notify owner on close",
    payload=Path("automation/notify_on_close.json"),
    refs={"severity": severity},
    enabled=True,
)

trk_rules = AutomationRuleScheme(
    alias="trk_rules",
    name="TRK Automation",
    rules=[trk_guard, notify_on_close],
)


class Tracker(Project):
    __key__ = "TRK"
    __issuetypes__ = [Bug]
    __roles__ = {
        # The service account (and the admins) in the role the permission
        # scheme and the guard's allow-list reference.
        "Administrators": [User("svc-automation@example.com")],
    }
    __automation_rule_scheme__ = trk_rules
