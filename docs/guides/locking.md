# Lock it for users, not for the service

The common pattern: a service account manages a Jira project end to end
through stint (schema, issues, transitions), while **regular users must not
change certain things** — the status, or the "control" fields the automation
depends on. They can still read the issues and work in them.

Each row below names the Jira mechanism and how stint expresses it.

| Want to stop regular users from... | Mechanism | stint |
|---|---|---|
| editing specific fields in the UI | restricted Edit screen in the `ScreenScheme` | supported |
| editing issues at all | permission scheme `EDIT_ISSUES` | `PermissionScheme` |
| changing status, or using a specific transition | workflow transition conditions; permission scheme `TRANSITION_ISSUES` | `TRANSITION_ISSUES` via `PermissionScheme` (workflow conditions are a future feature) |
| seeing issues | issue security level / permission scheme `BROWSE_PROJECTS` | security schemes / `PermissionScheme` |
| (keep the service account able to do all of it) | project role actors + lockout guards | `__roles__` + the lockout guard |

!!! note
    A restricted Edit screen locks fields in the UI (the UI drives inline
    editing from `editmeta`, so locked fields drop out of it). Privileged
    REST callers can still write those fields — which is desirable for the
    owning service account, and is exactly why the permission scheme, not
    the screen, is the real control for API clients.

## Pinned platform behavior: the screen lock is UI-only

On current Jira Cloud (company-managed projects), a field absent from the
Edit screen is **still editable through the REST API** by anyone holding
`EDIT_ISSUES`, and **Rovo** exposes that to every licensed user in plain
language. This changed with the deprecation of override screen security and
the project/field association changes; track it at
[JRACLOUD-98054](https://jira.atlassian.com/browse/JRACLOUD-98054). Native
per-field, per-identity permissions do not exist (the long-standing request
JRACLOUD-69298 was declined in December 2024); the Marketplace ecosystem
(Protected Custom Fields, Secure Fields, FieldLock) is the only source of
true field locks.

So the restricted Edit screen is a *behavioral* guard: it narrows `editmeta`,
so well-behaved agents (stint's own data plane, MCP servers that follow
metadata) never touch the field. It stops nothing hostile.

What IS enforced server-side for every caller:

- permission-scheme operations (`EDIT_ISSUES`, `TRANSITION_ISSUES`, ...),
- workflow transition conditions and validators,
- issue security levels,
- workflow status properties (`jira.permission.edit.denied`,
  `jira.issue.editable=false`, ... — status-scoped, whole-issue),
- the create screen (uniform, not per-identity).

Pin the behavior on your own tenant with the bundled script (run against a
throwaway issue; `--apply` writes):

```
python scripts/verify_screen_lock.py \
    --url https://you.atlassian.net \
    --issue SEC-1 \
    --field customfield_10042 \
    --value-json '{"value": "S1"}' \
    --apply
```

Expected today: the field is absent from `editmeta` for both identities, and
both `PUT`s succeed — the restricted identity included, whenever it holds
`EDIT_ISSUES`. If your tenant still rejects, record that instead of
assuming; re-run after Atlassian ships anything on JRACLOUD-98054.

`engine.audit_control_fields` closes the loop the schema cannot express: it
derives the control fields (create/view-screen fields the Edit screen
omits), scans changelog entries since a bound, and reports every writer
outside an allow-list. `engine.check` warns when a declared Edit screen
excludes control fields while the live permission scheme grants
`EDIT_ISSUES` — the exact combination where the UI lock looks real but is
cosmetic for API clients.

## One complete example

```python
from stint import (
    FieldConfiguration,
    Group,
    PermissionScheme,
    Project,
    ProjectRole,
    Screen,
    ScreenScheme,
    User,
)

# 1. Screens: a narrow Edit screen keeps control fields out of the UI.
from stint import IssueType

view_screen = Screen(alias="tracker_view", name="Tracker View", fields=["Summary", "Description", "Status"])
edit_screen = Screen(alias="tracker_edit", name="Tracker Edit", fields=["Summary", "Description"])
screens = ScreenScheme(
    alias="tracker_screens", name="Tracker Screens", create=view_screen, edit=edit_screen, view=view_screen
)


class Bug(IssueType):
    __alias__ = "bug"
    __title__ = "Bug"
    __screen_scheme__ = screens


# 2. Permission scheme: users browse and comment; only the service role
#    creates/edits/transitions/administers.
tracker_permissions = PermissionScheme(
    alias="tracker_permissions",
    name="Tracker Permission Scheme",
    grants={
        "BROWSE_PROJECTS": [ProjectRole("Viewers"), ProjectRole("Members"), ProjectRole("Administrators")],
        "ADD_COMMENTS": [ProjectRole("Viewers"), ProjectRole("Administrators")],
        "CREATE_ISSUES": [ProjectRole("Administrators")],
        "EDIT_ISSUES": [ProjectRole("Administrators")],
        "TRANSITION_ISSUES": [ProjectRole("Administrators")],
        "ADMINISTER_PROJECTS": [ProjectRole("Administrators")],
    },
)


class Tracker(Project):
    __key__ = "TRK"
    __issuetypes__ = [Bug]
    __permission_scheme__ = tracker_permissions
    __roles__ = {
        "Administrators": [User("svc-automation@example.com")],  # the service account
        "Viewers": [Group("all-staff")],
    }
```

## Why the lockout guard is part of the recipe

Every piece above can remove the service account's own access: dropping it
from `Administrators`, or narrowing `BROWSE_PROJECTS`/`ADMINISTER_PROJECTS`
to holders it doesn't belong to, leaves stint unable to manage — or even
see — a project it owns. stint evaluates every planned role-actor removal
and grant change against the account's verifiable memberships (its roles in
the project, its groups) and refuses the plan when a required permission
would be lost. See [Schema as code](schema.md#lockout-guard-never-lock-out-the-owning-account).
