"""Example stint schema for the 'lock it for users, not for the service' recipe.

A service account manages a TRK Jira project end to end through stint, while
regular users can read and comment but must not edit issues or change their
status. Demonstrates:

- A restricted Edit screen (locks fields in the UI via editmeta; privileged
  REST callers can still write them — see docs/guides/locking.md).
- A PermissionScheme that grants BROWSE/ADD_COMMENTS widely and
  EDIT/TRANSITION/CREATE/ADMINISTER only to the service role.
- Project __roles__ declaring who is in each role, including the service
  account. Roles not declared here are left unmanaged.

The permission-scheme grants reference ProjectRole holders, so the role
memberships declared in __roles__ are what make the scheme effective —
and the lockout guard refuses any plan that would strip the service
account of its required permissions.

Run `stint validate --schema examples.company_managed.permission_scheme`
(no network) to confirm internal consistency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from stint import (
    CustomField,
    FieldConfiguration,
    Group,
    IssueType,
    PermissionScheme,
    Project,
    ProjectRole,
    Screen,
    ScreenScheme,
    SelectField,
    TextField,
    User,
)

# ── Custom fields ───────────────────────────────────────────────────────
business_impact_field = CustomField(
    alias="ticket_business_impact",
    name="Business Impact",
    type=SelectField,
    description="Impact classification set by the automation",
    options=["Low", "Medium", "High"],
)

notes_field = CustomField(
    alias="ticket_notes",
    name="Notes",
    type=TextField,
)

# ── Screens ─────────────────────────────────────────────────────────────
# The Edit screen omits Business Impact: regular users cannot change the
# control field the automation depends on. The View screen still shows it.
# (A restricted Edit screen locks fields in the UI only; the permission
# scheme below is the real control for API clients.)
ticket_view_screen = Screen(
    alias="ticket_view",
    name="TRK View Screen",
    fields=["Summary", "Description", "Assignee", business_impact_field, notes_field, "Status", "Created", "Updated"],
)

ticket_edit_screen = Screen(
    alias="ticket_edit",
    name="TRK Edit Screen",
    fields=["Summary", "Description", "Assignee", notes_field],
)

ticket_create_screen = Screen(
    alias="ticket_create",
    name="TRK Create Screen",
    fields=["Summary", "Description", "Assignee", business_impact_field, notes_field],
)

ticket_screen_scheme = ScreenScheme(
    alias="ticket_screens",
    name="TRK Screen Scheme",
    create=ticket_create_screen,
    edit=ticket_edit_screen,
    view=ticket_view_screen,
)

# ── Field configuration ────────────────────────────────────────────────
ticket_field_config = FieldConfiguration(
    alias="ticket_fields",
    name="TRK Field Configuration",
    required=["Summary"],
)

# ── Permission scheme ──────────────────────────────────────────────────
# Regular users: browse and comment. The service role: everything else.
# ProjectRole holders resolve against the roles declared in __roles__.
tracker_permissions = PermissionScheme(
    alias="tracker_permissions",
    name="Tracker Permission Scheme",
    grants={
        "BROWSE_PROJECTS": [
            ProjectRole("Viewers"),
            ProjectRole("Administrators"),
        ],
        "ADD_COMMENTS": [
            ProjectRole("Viewers"),
            ProjectRole("Administrators"),
        ],
        "CREATE_ISSUES": [ProjectRole("Administrators")],
        "EDIT_ISSUES": [ProjectRole("Administrators")],
        "TRANSITION_ISSUES": [ProjectRole("Administrators")],
        "ADMINISTER_PROJECTS": [ProjectRole("Administrators")],
    },
)


# ── Issue type ──────────────────────────────────────────────────────────
class Ticket(IssueType):
    __alias__ = "ticket"
    __title__ = "Ticket"
    __description__ = "Work tracked by the tracker service"
    __screen_scheme__ = ticket_screen_scheme
    __field_configuration__ = ticket_field_config

    summary: str
    description: str | None = None
    assignee: str | None = None
    business_impact: Annotated[Literal["Low", "Medium", "High"], business_impact_field]
    notes: Annotated[str | None, notes_field] = None
    created: datetime | None = None
    updated: datetime | None = None


# ── Project ─────────────────────────────────────────────────────────────
class Tracker(Project):
    __key__ = "TRK"
    __lead__ = "svc-automation@example.com"
    __style__ = "company-managed"
    __issuetypes__ = [Ticket]
    __permission_scheme__ = tracker_permissions

    # Declared role memberships: the service account runs the project, a
    # tenant-wide group can view and comment. Roles not listed here are left
    # unmanaged, so adopting stint never wipes existing memberships.
    # The lockout guard refuses a plan that would remove the service
    # account's last role granting BROWSE/ADMINISTER (see docs/guides/schema.md).
    __roles__ = {
        "Administrators": [User("svc-automation@example.com")],
        "Viewers": [Group("all-staff")],
    }
