# Schema as code

Everything in the **schema plane** is a Python declaration in your repo. stint imports it, registers every object under its alias, validates cross-references, and turns it into migration operations. There are no decorators: declarativeness comes from base classes with metaclasses (`IssueType`, `Project`), and dataclasses that self-register on construction (`CustomField`, `Screen`, `ScreenScheme`, `FieldConfiguration`, security-scheme types).

## Custom fields

`CustomField` binds a stable Python-side **alias** to a Jira custom field:

```python
from stint import CustomField, MultiSelectField, SelectField, TextField, UserField

severity_field = CustomField(
    alias="bug_severity",
    name="Severity",
    type=SelectField,  # a field-type class, not an instance
    description="Bug severity classification",
    options=["S1", "S2", "S3", "S4"],
)

tags_field = CustomField(alias="release_tags", name="Tags", type=MultiSelectField)
```

- `alias` is the stable name used in imports, state files, and migrations — Jira's numeric field ids change per domain.
- `type` maps to a Jira customfield type **plus** its JQL searcher key; without the right searcher, JQL clauses over the field fail.
- `options` may only be given to (and are required by) elective types: `SelectField`, `MultiSelectField`, `RadioButtonsField`, `CheckboxesField`.

### Field types

`stint.fields` provides 18 marker classes, each carrying its Jira type id and searcher key:

| Category | Types |
|---|---|
| Text | `TextField`, `TextAreaField`, `ReadOnlyField` |
| Selection (need `options`) | `SelectField`, `MultiSelectField`, `RadioButtonsField`, `CheckboxesField` |
| Numbers / dates | `NumberField`, `DateField`, `DateTimeField` |
| People | `UserField`, `MultiUserField`, `GroupField`, `MultiGroupField` |
| Links / versions | `URLField`, `VersionField`, `MultiVersionField` |
| Freeform | `LabelsField` |

## Screens, screen schemes, field configuration

These are company-managed concepts. `Screen` is an ordered list of fields — strings are Jira system field names, `CustomField` instances are your declared fields:

```python
from stint import FieldConfiguration, Screen, ScreenScheme

bug_create_screen = Screen(
    alias="bug_create",
    name="Bug Create Screen",
    fields=["Summary", "Description", "Reporter", severity_field],
)

bug_screen_scheme = ScreenScheme(
    alias="bug_screens",
    name="Bug Screen Scheme",
    create=bug_create_screen,  # CMP: separate screens per operation
    edit=bug_edit_screen,
    view=bug_view_screen,
)

bug_field_config = FieldConfiguration(
    alias="bug_fields",
    name="Bug Field Configuration",
    required=["Summary", "Reporter", severity_field],
    hidden=[],  # optional
)
```

## Issue types

`IssueType` subclasses play a **dual role**: Jira-side schema metadata (`__alias__`, `__screen_scheme__`, `__field_configuration__`, ...) and a Pydantic model for the [data plane](querying.md).

```python
from typing import Annotated, Literal
from stint import IssueType


class Bug(IssueType):
    __alias__ = "bug"  # required, stable identity
    __title__ = "Bug"  # defaults to the class name
    __description__ = "Defects in production"
    __screen_scheme__ = bug_screen_scheme
    __field_configuration__ = bug_field_config

    summary: str  # system field
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]  # custom field
    root_cause: Annotated[str | None, root_cause_field] = None  # optional custom field
```

Rules the metaclass enforces at definition time:

- `__alias__` is required and must be unique (re-declaring from the *same module* replaces — that keeps imports from clobbering; a clash from a different module raises `RegistryError`).
- Custom-field attributes **must** use `Annotated[T, <CustomField instance>]` so the schema plane can find them.
- Constraining literals (e.g. `Literal["S1", ...]`) ride along: constructed instances are Pydantic-validated against them.

## Projects

```python
from stint import Project


class Platform(Project):
    __key__ = "PLAT"  # required
    __title__ = "Platform"
    __lead__ = "cturner@example.com"
    __lead_role__ = "Browse"  # optional; see note below
    __style__ = "company-managed"  # or the experimental "team-managed"
    __issuetypes__ = [Bug]
```

!!! note
    `__lead__` requires the token owner to hold the "Browse users and groups" permission. Built-in issue types (`Bug`, `Task`, ...) that you reference are **adopted**, not recreated.

!!! note
    `POST /project` grants the lead no project role. Tenants whose default
    permission scheme grants `BROWSE_PROJECTS` only through project roles
    leave the lead unable to see the project they own (Jira's own UI flow
    adds the creator to a role; the REST create does not). Set
    `__lead_role__` to an existing project role name and stint adds the
    lead to it during apply. Company-managed projects only.

## Security schemes

Issue-security schemes are declared as dataclasses; emails in members are resolved to account ids at apply time:

```python
from stint import SecurityLevel, SecurityLevelMember, SecurityScheme

internal_level = SecurityLevel(
    name="Internal Only",
    members=[SecurityLevelMember(type="group", name="platform-team")],
)
public_level = SecurityLevel(
    name="Public",
    members=[SecurityLevelMember(type="user", name="cturner@example.com")],
)

bug_security = SecurityScheme(
    alias="bug_security",
    name="Bug Security",
    levels=[internal_level, public_level],
    default_level=internal_level,
)
```

Wire it to a project through `op.set_project_issue_security_scheme` in a migration.

## Project roles (`__roles__`)

Permission schemes and workflow conditions grant access to *roles*, so role
membership is the lever that ties the security model together. Declare who is
in which role on the project:

```python
from stint import Project, Group, User


class Tracker(Project):
    __key__ = "TRK"
    __issuetypes__ = [Bug]
    __roles__ = {
        "Administrators": [User("svc-automation@example.com")],
        "Viewers": [Group("all-staff")],
    }
```

Behavior:

- Emails resolve to accountIds at apply time (same user-search API as the
  project lead); a raw accountId passes through. Group names pass through.
- stint reflects each declared role's live actors and diffs membership into
  add/remove actor ops. Actor **removals are destructive**: they are emitted
  only with `--allow-delete`.
- Roles that are **not declared are left alone** (unmanaged), so adopting
  stint on an existing project never wipes its memberships.
- Declaring the lead's role here (with the lead as a `User` actor) is the
  general form of `__lead_role__` — don't set both for the same role.
- Company-managed projects only.

## Permission schemes

"Regular users can read and comment but not edit or transition" is a
permission-scheme statement: `EDIT_ISSUES` and `TRANSITION_ISSUES` granted to
a narrow role, `BROWSE_PROJECTS` and `ADD_COMMENTS` to a wide one.

```python
from stint import PermissionScheme, ProjectRole, Group, User

tracker_permissions = PermissionScheme(
    alias="tracker_permissions",
    name="Tracker Permission Scheme",
    copy_from="Default software scheme",  # optional: seed from an existing scheme
    grants={
        "BROWSE_PROJECTS": [ProjectRole("Viewers"), ProjectRole("Administrators")],
        "ADD_COMMENTS": [ProjectRole("Viewers"), ProjectRole("Administrators")],
        "CREATE_ISSUES": [ProjectRole("Administrators")],
        "EDIT_ISSUES": [ProjectRole("Administrators")],
        "TRANSITION_ISSUES": [ProjectRole("Administrators")],
        "ADMINISTER_PROJECTS": [ProjectRole("Administrators")],
    },
)


class Tracker(Project):
    __permission_scheme__ = tracker_permissions
    __roles__ = {
        "Administrators": [User("svc-automation@example.com")],
        "Viewers": [Group("all-staff")],
    }
```

Holder types: `ProjectRole(name)`, `Group(name)`, `User(email-or-accountId)`,
`ApplicationRole(name)` (any logged-in user when unnamed), `Reporter`,
`Assignee`, `ProjectLead`. Grant keys must be built-in Jira permission keys;
`stint validate` rejects unknown ones.

Grant policy (explicit, no silent default):

- Declared grants are always managed: added when missing.
- With `copy_from`, grants inherited from the source scheme are **unmanaged**
  — they stay even when undeclared, and `engine.check` reports them.
- Without `copy_from`, the scheme is exactly what is declared: undeclared
  live grants are drift, and removing them needs `--allow-delete`.
- The project binding (`PUT /project/{key}/permissionscheme`) is diffed like
  every other scheme binding.

`stint validate` cross-checks `ProjectRole` holders against the project's
declared `__roles__` so a typo'd role name doesn't grant to nobody.

## Workflows and workflow schemes

"Regular users can't move this to Done" is a *transition condition*: restrict
the transition to a role, group, user, or permission. Statuses are
tenant-global (referenced by name across workflows); workflows are
company-managed objects referenced by name; a workflow scheme binds a default
workflow plus per-issuetype overrides to the project.

```python
from stint import Status, Transition, Workflow, WorkflowScheme, ProjectRole, Permission

open_ = Status("Open", category="new")
in_progress = Status("In Progress")
done = Status("Done", category="done")

tracker_flow = Workflow(
    alias="tracker_flow",
    name="Tracker Workflow",
    statuses=[open_, in_progress, done],
    initial=open_,
    transitions=[
        Transition("Start", from_=[open_], to=in_progress),
        Transition("Close", from_=[in_progress], to=done, restrict_to=[ProjectRole("Administrators")]),
        Transition("Reopen", from_=[done], to=open_, restrict_to=[Permission("ADMINISTER_PROJECTS")]),
    ],
)

tracker_wfs = WorkflowScheme(alias="tracker_wfs", name="Tracker WFS", default=tracker_flow)


class Tracker(Project):
    __workflow_scheme__ = tracker_wfs
```

Restrict holders: `ProjectRole(name)`, `Group(name)`, `User(email)`,
`Permission(key)`. They all map to one aggregate rule
(`system:restrict-issue-transition`) whose criteria are OR-ed — a role member
*or* a group member *or* someone holding the permission may transition.
Role names resolve to Jira role ids and group names to group ids at apply
time; unknown names fail loudly instead of restricting to nobody.

Declared vs. undeclared, per object:

- **Adoption is non-destructive.** A workflow or scheme that already exists
  in Jira is recorded into state without rewriting it: undeclared
  transitions, validators, and postfunctions are preserved (unmanaged), and
  `engine.check` reports drift.
- **Updates are declarative but preserving.** When declared content drifts,
  the update payload sends declared transitions plus every undeclared live
  status and transition verbatim, so Jira's full-replace update semantics
  never delete what the schema doesn't manage.
- **Orphans** (tracked in state, gone from the schema) delete behind
  `--allow-delete` — schemes first, then workflows.
- Jira's implicit `Create` transition into the initial status is always sent
  explicitly; the platform does not auto-add it.

## Workflow scheme binding and status mappings

Binding a scheme to a project being created in the same migration is the
synchronous associate path. Rebinding an **existing** project is different:
Jira runs an async switch task, and statuses that would vanish under the
target scheme require explicit `status_mappings` in the migration.

stint never guesses a mapping. Before any write, the op asks Jira which
statuses would be stranded (`POST /workflowscheme/update/mappings`); if any
are, the op refuses and lists them, and you fill the mapping:

```python
await op.set_project_workflow_scheme(
    project_alias="TRK",
    scheme_alias="tracker_wfs",
    live=True,
    status_mappings={"Legacy Status": "Done"},  # old status name -> new status name
)
```

The task is polled to completion (`GET /task/{id}`; COMPLETE/FAILED), so an
apply either finished the migration or failed visibly.

## Transition lockout guard

Workflow changes can take away a transition the account stint authenticates
as can use today — and services that drive `session.transition()` stop
working. Both the plan-time check (`engine.check`) and the apply-time ops
evaluate: of every transition the account provably can use under the current
restrict set (roles via its project memberships, groups via group-id
resolution, permissions via `/mypermissions`), none may become unusable.
Escape with `--allow-lockout`, like every other guard. Role and permission
holders are only verifiable with project context — bare
`op.update_workflow` calls verify group/account holders; scheme switches
verify everything.

## Lockout guard: never lock out the owning account

Every role-actor removal and permission-grant change can remove the account
stint authenticates as from the project it manages. stint refuses to plan
(and ops refuse to apply) a change that would leave that account without:

- `BROWSE_PROJECTS` and `ADMINISTER_PROJECTS` (role-actor removals), or
- `BROWSE_PROJECTS`, `ADMINISTER_PROJECTS`, `EDIT_ISSUES`,
  `TRANSITION_ISSUES`, `CREATE_ISSUES` (permission-scheme changes).

The evaluation is conservative: group holders count only when the account's
membership is verifiable (its groups are fetched via the user API), and
per-issue holders (`reporter`, `assignee`) never count as evidence of
retention. Escape hatch when you really mean it: `--allow-lockout` on
`stint revision --autogenerate` (baked into the migration's remove ops), or
`allow_lockout=True` on `op.remove_project_role_actor` /
`op.remove_permission_grant` in hand-written migrations. Override the
required set with `--required-permissions`.

## Automation rules and the control-field guard

Some fields are *control* fields: the schema puts them on the create/view
screens and keeps them off the edit screen, so regular users cannot change
them in the UI. On Jira Cloud that lock is cosmetic — since the
override-screen-security deprecation, the REST API ignores screens on edit
(JRACLOUD-98054), and anyone holding `EDIT_ISSUES` can write the field
anyway. The practical control left is detect-and-revert with Jira
Automation, and stint manages those rules like every other scheme object:
declared, reflected, diffed, applied, recorded in state by uuid.

```python
from stint import AutomationRule, AutomationRuleScheme, ControlFieldGuard, ProjectRole, User

guard = ControlFieldGuard(
    alias="trk_control_guard",
    name="TRK: revert control-field edits",
    # Default: the control fields derived from the bound project's screens
    # (create/view minus edit). Pass fields= to add or narrow.
    allowed_writers=[User("svc-automation@example.com"), ProjectRole("Administrators")],
    on_violation=["revert", "comment"],  # revert, then note it on the issue
)

# Escape hatch: a rule exported from the Automation UI, with tenant ids
# replaced by stint references. stint substitutes them at apply time and
# fails validation on an unknown one.
notify_on_close = AutomationRule(
    alias="trk_notify_on_close",
    name="TRK: notify owner on close",
    payload="automation/notify_on_close.json",  # relative to this file
    refs={"severity": severity},  # "{{stint.field.severity}}" -> customfield_NNNNN
    enabled=True,
)

trk_rules = AutomationRuleScheme(alias="trk_rules", name="TRK Automation", rules=[guard, notify_on_close])


class Tracker(Project):
    __automation_rule_scheme__ = trk_rules  # every rule scopes to the bound projects
```

How the generated guard works:

- **Trigger**: Field value changed on exactly the guarded field ids,
  filtered to edit operations. Create stays allowed (the create screen is
  the authoring surface).
- **Condition**: the initiator is not one of the allowed writers — the
  native user condition (users, groups, project roles). "Allow rule
  trigger" stays off, so the revert cannot re-trigger the rule; Jira's
  loop detection (10 rapid triggers) is not a substitute.
- **Revert**: an Edit issue action per guarded field restoring the raw
  previous value from the changelog (`{{#changelog.<id>}}{{from}}{{/}}`) —
  option ids for selects, full previous sets for multi-value fields,
  accountIds for user fields — with notifications off, so the bad value
  does not page watchers twice. Then the optional comment, mentioning the
  initiator.

The revert happens after the fact: the bad value exists for seconds, and
both changes appear in history, webhooks, and notifications. Automation
usage is billed in rule runs that perform actions, so on a busy project a
guard costs one run per blocked edit.

Cross-cutting rule: **never let the guard fight the service.** The
authenticated account (the service account) must be among the guard's
`allowed_writers` — `stint validate` flags it and the ops refuse to apply
a guard that would revert the service's own writes (escape with
`--allow-lockout`). The rule's actor is always allowed.

Diff and apply behavior, same rules as the other scheme families:
adoption by exact name (or uuid from state) never rewrites; the payload
diff normalizes server-generated noise so an unchanged schema re-runs
clean; enabled-state and project scope have their own ops (one
multi-project scope, never copies); orphans delete behind
`--allow-delete`, disabled first; undeclared rules on the tenant are left
unmanaged. The rule-management API requires an API token (Forge and OAuth
apps cannot call it) and an administrator-level account — stint probes
that with a read before writing anything and stops on a 403 with a clear
message.

`engine.check` uses the guard to split the cosmetic-lock warning: control
fields with no enabled guard covering them warn as before, covered fields
report as info naming the rule, and a drifted guard (disabled in Jira,
fields no longer matching the screens, or an allow-list that no longer
includes the authenticated account) warns. `engine.audit_control_fields`
accepts the guard as the allow-list source and classifies each
out-of-band write as reverted (a restore followed) or unreverted — the
ones a service should alert on.

## Validation and the registry

- [`stint.validate()`](../api/validate.md) collects human-readable problems (project → issue type, screen → custom field, scheme → screen, ...) without raising.
- [`stint.validate_or_raise()`](../api/validate.md) raises `ConfigurationError` on the first batch of problems.
- The [`registry`](../api/registry.md) singleton tracks every declared object by alias; the CLI `stint validate` command runs checks against a schema module with **no network calls**.

Everything here lands in the [state file](migrations.md#the-state-file) when migrations apply — the alias → id map that powers migrations and JQL later.
