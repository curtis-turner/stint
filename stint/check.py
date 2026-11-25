"""Runtime schema check: a read-only drift report a long-running service
can call at startup.

``engine.check(Project, state)`` reflects the tenant (one reflect's worth of
reads, no writes) plus the create-metadata endpoint, then compares what the
code declares against what Jira actually has. It answers the question
`stint validate` cannot: does the project STILL match, right now — before
the first insert 400s mid-sync because someone edited the project in the UI.

Findings split by severity:

- **errors** — the declared schema cannot work against this tenant as-is:
  a declared custom field or option is missing, an issuetype is gone or not
  on the project, a field is not on the create screen.
- **warnings** — drift worth knowing about but not fatal: extra options on
  the tenant, renamed issuetype/field descriptions.

``report.raise_for_errors()`` turns errors into a ConfigurationError; the
CLI (`stint check`) exits non-zero on the same condition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stint.exceptions import ConfigurationError
from stint.fields import (
    CheckboxesField,
    CustomField,
    MultiSelectField,
    RadioButtonsField,
    SelectField,
)

if TYPE_CHECKING:
    from stint.engine import Engine
    from stint.schema.issuetype import IssueType
    from stint.schema.project import Project
    from stint.state.file import StateFile

_SELECT_STYLES = (SelectField, MultiSelectField, RadioButtonsField, CheckboxesField)

# Attributes never expected on a create screen: derived/read-only, set
# through transitions, or stripped by stint's own insert builder.
_CREATE_EXEMPT = frozenset({"key", "status", "status_category", "created", "updated", "resolution", "reporter"})


@dataclass
class CheckReport:
    """Findings for one declared project. ``errors`` mean the declared
    schema cannot work against the live tenant; ``warnings`` do not block."""

    project: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            details = "\n".join(f"  - {e}" for e in self.errors)
            raise ConfigurationError(
                f"schema check failed for project {self.project!r} ({len(self.errors)} error(s)):\n{details}"
            )


async def check_project(engine: Engine, project_cls: type[Project], state: StateFile) -> CheckReport:
    """Reflect the tenant and compare the declared project against it.

    Read-only: one full reflect plus one createmeta GET. No state is
    written, nothing is applied.
    """
    report = CheckReport(project=project_cls.__key__)
    dialect = engine.dialect
    snapshot = await dialect.reflect()

    project_key = project_cls.__key__
    proj_snap = snapshot.projects.get(project_key)
    if proj_snap is None:
        report.errors.append(f"project {project_key!r} does not exist on the tenant")
    scheme_members: set[str] | None = None
    if proj_snap is not None and proj_snap.issuetype_scheme_id:
        scheme = snapshot.issuetype_schemes.get(proj_snap.issuetype_scheme_id)
        if scheme is not None:
            scheme_members = set(scheme.issuetype_ids)

    create_fields = await _createmeta_fields(dialect, project_key)

    issuetypes: list[type[IssueType]] = list(getattr(project_cls, "__issuetypes__", ()) or ())
    for it in issuetypes:
        _check_issuetype(it, state, snapshot, report, scheme_members, create_fields)
    await _check_roles(dialect, project_cls, state, snapshot, report)
    await _check_permission_scheme(dialect, project_cls, state, snapshot, report)
    _check_screen_lock_coverage(dialect, project_cls, state, snapshot, report)
    await _check_workflows(dialect, project_cls, state, snapshot, report)
    return report


_ROLE_LOCKOUT_NOTE = "the service account may lose access it relies on"


async def _check_roles(
    dialect: Any,
    project_cls: type[Project],
    state: Any,
    snapshot: Any,
    report: CheckReport,
) -> None:
    """Compare declared ``__roles__`` memberships against the snapshot."""
    roles = getattr(project_cls, "__roles__", None)
    if not roles:
        return
    proj_snap = snapshot.projects.get(project_cls.__key__)
    if proj_snap is None:
        return  # project existence already reported
    for role_name, actors in roles.items():
        live = proj_snap.roles.get(role_name)
        if live is None:
            report.errors.append(f"role {role_name!r} declared in __roles__ does not exist on project {report.project}")
            continue
        live_params = {(a.type, a.parameter) for a in live if a.parameter}
        declared_params: set[tuple[str, str]] = set()
        for actor in actors:
            actor_type = "user" if type(actor).__name__ == "User" else "group"
            declared_value = actor.value if actor_type == "user" else actor.name
            try:
                parameter = await dialect.resolve_user(declared_value) if actor_type == "user" else declared_value
            except ConfigurationError as exc:
                report.warnings.append(f"role {role_name!r}: cannot resolve actor {declared_value!r}: {exc}")
                continue
            declared_params.add((actor_type, parameter))
            if (actor_type, parameter) not in live_params:
                report.errors.append(
                    f"role {role_name!r}: actor {actor_type} {declared_value!r} is not in the "
                    f"live role ({_ROLE_LOCKOUT_NOTE})"
                )
        for actor_type, parameter in sorted(live_params - declared_params):
            report.warnings.append(
                f"role {role_name!r}: actor {actor_type} {parameter!r} is in the live role but "
                f"not declared in __roles__"
            )


def _check_screen_lock_coverage(
    dialect: Any,
    project_cls: type[Project],
    state: Any,
    snapshot: Any,
    report: CheckReport,
) -> None:
    """Detect the "cosmetic lock" combination: a restricted Edit screen
    (declared control fields) while ``EDIT_ISSUES`` is granted broadly.

    The Edit screen is no longer an API gate on Jira Cloud: since the
    override-screen-security deprecation, fields off the edit screen are
    still editable through the REST API (JRACLOUD-98054), and Rovo exposes
    that to every licensed user. The permission scheme is the real control,
    so when the schema declares control fields, surface exactly who holds
    ``EDIT_ISSUES`` — the person who can judge whether that is intentional.
    """
    from stint.audit import declared_control_fields

    control = declared_control_fields(project_cls)
    if not control:
        return
    proj_snap = snapshot.projects.get(project_cls.__key__)
    if proj_snap is None:
        return
    scheme_snap = snapshot.permission_schemes.get(proj_snap.permission_scheme_id or "")
    if scheme_snap is None:
        return
    grants = [g for g in scheme_snap.grants if g.permission == "EDIT_ISSUES"]
    if not grants:
        return  # narrow (or absent): the lock holds for API callers
    roles_by_id = {role_id: name for name, role_id in (snapshot.project_roles or {}).items()}

    def _holder(g: Any) -> str:
        htype = g.holder_type.lower()
        if htype == "projectrole":
            return f"project role {roles_by_id.get(g.holder_parameter, g.holder_parameter)!r}"
        if htype == "group":
            return f"group {g.holder_parameter!r}"
        if htype == "user":
            return f"user {g.holder_parameter!r}"
        if htype == "applicationrole":
            return f"application role {g.holder_parameter!r}" if g.holder_parameter else "any logged-in user"
        return f"{g.holder_type} {g.holder_parameter!r}".strip()

    fields = sorted({name for names in control.values() for name in names})
    report.warnings.append(
        f"project {report.project}: control field(s) {fields} are locked in the UI "
        f"(absent from a declared Edit screen) but the live permission scheme grants "
        f"EDIT_ISSUES to {', '.join(_holder(g) for g in grants)}. Screen-locked fields "
        f"are editable through the REST API (JRACLOUD-98054) by anyone holding "
        f"EDIT_ISSUES, and Rovo exposes that to every licensed user — narrow the grant "
        f"or the restricted Edit screen is cosmetic for API clients."
    )


async def _check_workflows(
    dialect: Any,
    project_cls: type[Project],
    state: Any,
    snapshot: Any,
    report: CheckReport,
) -> None:
    """Workflow drift: binding, statuses, transitions, and the transition
    lockout evaluation (read-only — runs the same restrict comparison the
    apply-time guard runs, so a schema change that would strand the
    service account's transitions surfaces at check time too)."""
    from stint.lockout import (
        lost_transitions,
        restrict_rule_parameters,
    )

    scheme_alias = getattr(getattr(project_cls, "__workflow_scheme__", None), "alias", "")
    if not scheme_alias:
        return
    proj_snap = snapshot.projects.get(project_cls.__key__)
    if proj_snap is None:
        return  # project existence already reported
    scheme_state = state.workflow_schemes.get(scheme_alias)
    scheme_snap = snapshot.workflow_schemes.get(proj_snap.workflow_scheme_id or "")
    if scheme_state is None or scheme_snap is None:
        return  # not yet applied: the diff will create/adopt/bind
    if scheme_snap.id != scheme_state.id:
        report.errors.append(
            f"workflow_scheme {scheme_alias!r}: project binds scheme {proj_snap.workflow_scheme_id!r} "
            f"but state records {scheme_state.id!r} (out-of-band rebind?)"
        )
    if scheme_snap.default_workflow and scheme_snap.default_workflow != scheme_state.default_workflow:
        pass  # default drift is name-vs-alias space; the diff covers rebinds
    declared_wf_aliases = {scheme_state.default_workflow, *scheme_state.mappings.values()}
    declared_wf_aliases.discard("")
    for wf_alias in sorted(declared_wf_aliases):
        wf_state = state.workflows.get(wf_alias)
        if wf_state is None:
            continue
        live = snapshot.workflows.get(wf_state.name)
        if live is None:
            live = next((w for w in snapshot.workflows.values() if w.id == wf_state.id), None)
        if live is None:
            report.errors.append(
                f"workflow {wf_alias!r}: state references id {wf_state.id!r} but no workflow "
                f"named {wf_state.name!r} is live in Jira (out-of-band delete?)"
            )
            continue
        project_cls_workflow = _declared_workflow(project_cls, wf_alias)
        if project_cls_workflow is None:
            continue
        declared_status_names = {s.name for s in project_cls_workflow.statuses}
        live_status_names = {s.name for s in live.statuses}
        for missing in sorted(declared_status_names - live_status_names):
            report.errors.append(f"workflow {wf_alias!r}: declared status {missing!r} is not on the live workflow")
        declared_transitions = {t.name: t for t in project_cls_workflow.transitions}
        live_transitions = {t.name: t for t in live.transitions}
        for name in sorted(set(declared_transitions) - set(live_transitions)):
            report.errors.append(
                f"workflow {wf_alias!r}: declared transition {name!r} does not exist on the live workflow"
            )
        for name in sorted(set(live_transitions) - set(declared_transitions)):
            report.warnings.append(
                f"workflow {wf_alias!r}: transition {name!r} is on the live workflow but not declared "
                f"(preserved unmanaged; validators/postfunctions ride along)"
            )

    # Transition lockout evaluation (read-only): declared restrict vs live
    # restrict for every workflow the scheme references. Same evaluator the
    # apply-time guard runs; roles resolve via the account's project
    # memberships, groups via group-id lookup, permissions via
    # /mypermissions.
    account_id = await dialect.my_account_id()
    account_groups = await dialect.user_groups(account_id)
    group_ids = await dialect.resolve_group_ids(sorted(account_groups)) if account_groups else {}
    memberships = await dialect.project_role_memberships(proj_snap.id, account_id)
    global_roles = await dialect.global_project_role_ids()
    role_ids = {global_roles[n] for n in memberships if n in global_roles}
    before: dict[str, dict[str, str] | None] = {}
    after: dict[str, dict[str, str] | None] = {}
    permission_keys: set[str] = set()
    for wf_alias in sorted(declared_wf_aliases):
        wf_state = state.workflows.get(wf_alias)
        declared = _declared_workflow(project_cls, wf_alias)
        if wf_state is None or declared is None:
            continue
        live = snapshot.workflows.get(wf_state.name) or next(
            (w for w in snapshot.workflows.values() if w.id == wf_state.id), None
        )
        if live is None:
            continue
        for t in live.transitions:
            params = restrict_rule_parameters(t.conditions)
            before[f"{wf_state.name}:{t.name}"] = params
            if params:
                permission_keys.update(k.strip() for k in str(params.get("permissionKeys", "")).split(",") if k.strip())
        for t in declared.transitions:
            user_values = [h.value for h in t.restrict_to if type(h).__name__ == "User"]
            resolved_users: dict[str, str] = {}
            for value in user_values:
                if "@" in value:
                    try:
                        resolved_users[value] = await dialect.resolve_user(value)
                    except ConfigurationError:
                        resolved_users[value] = value
                else:
                    resolved_users[value] = value
            resolved = _declared_restrict_parameters(t.restrict_to, snapshot.project_roles, group_ids, resolved_users)
            after[f"{wf_state.name}:{t.name}"] = resolved
            if resolved:
                permission_keys.update(
                    k.strip() for k in str(resolved.get("permissionKeys", "")).split(",") if k.strip()
                )
    held: set[str] = set()
    keys = sorted(permission_keys)
    if keys:
        have = await dialect.my_project_permissions(proj_snap.id, keys)
        held = {key for key, ok in have.items() if ok}
    lost = lost_transitions(
        before,
        after,
        account_id=account_id,
        account_role_ids=role_ids,
        account_group_ids=set(group_ids.values()),
        held_permission_keys=held,
    )
    if lost:
        report.errors.append(
            f"applying the declared workflow restrict set would take away the authenticated "
            f"account's ability to use transition(s) {sorted(lost)}; regenerate with "
            f"--allow-lockout to force it"
        )


def _declared_restrict_parameters(
    holders: list[Any],
    role_name_to_id: dict[str, str],
    group_name_to_id: dict[str, str],
    resolved_users: dict[str, str],
) -> dict[str, str] | None:
    """Declared restrict holders → restrict-rule parameters (check-time
    form, best-effort). Unresolvable role/group names return None
    (conservatively unrestricted — the apply-time resolution is
    authoritative and raises on unknown names). ``resolved_users`` maps
    declared user values (emails) to accountIds, pre-resolved by the
    async caller."""
    roles = [h.name for h in holders if type(h).__name__ == "ProjectRole"]
    groups = [h.name for h in holders if type(h).__name__ == "Group"]
    users = [h.value for h in holders if type(h).__name__ == "User"]
    perms = [h.key for h in holders if type(h).__name__ == "Permission"]
    if not (roles or groups or users or perms):
        return None
    role_ids = [role_name_to_id.get(n) for n in roles]
    if roles and any(rid is None for rid in role_ids):
        return None
    group_ids = [group_name_to_id.get(n) for n in groups]
    if groups and any(gid is None for gid in group_ids):
        return None
    resolved: dict[str, str] = {}
    if role_ids:
        resolved["roleIds"] = ",".join(str(rid) for rid in role_ids)
    if group_ids:
        resolved["groupIds"] = ",".join(str(gid) for gid in group_ids)
    if users:
        resolved["accountIds"] = ",".join(resolved_users.get(v, v) for v in users)
    if perms:
        resolved["permissionKeys"] = ",".join(perms)
    return resolved or None


def _declared_workflow(project_cls: type[Project], wf_alias: str):
    """The declared Workflow object for an alias, searched through the
    project's bound WorkflowScheme (default + mappings)."""
    scheme = getattr(project_cls, "__workflow_scheme__", None)
    if scheme is None:
        return None
    if getattr(scheme.default, "alias", "") == wf_alias:
        return scheme.default
    for workflow in (scheme.mappings or {}).values():
        if getattr(workflow, "alias", "") == wf_alias:
            return workflow
    return None


async def _check_permission_scheme(
    dialect: Any,
    project_cls: type[Project],
    state: Any,
    snapshot: Any,
    report: CheckReport,
) -> None:
    """Compare the declared ``__permission_scheme__`` binding and grants
    against the snapshot."""
    scheme = getattr(project_cls, "__permission_scheme__", None)
    if scheme is None:
        return
    proj_snap = snapshot.projects.get(project_cls.__key__)
    if proj_snap is None:
        return
    mapping = state.permission_schemes.get(scheme.alias)
    if mapping is None:
        report.errors.append(
            f"permission scheme {scheme.alias!r} is not in state; run `stint stamp` or `stint upgrade` first."
        )
    elif proj_snap.permission_scheme_id is not None and proj_snap.permission_scheme_id != mapping.id:
        report.errors.append(
            f"project {report.project} is bound to permission scheme {proj_snap.permission_scheme_id!r} "
            f"but the schema binds {scheme.alias!r} ({mapping.id!r})"
        )
    scheme_snap = snapshot.permission_schemes.get(proj_snap.permission_scheme_id or "")
    if scheme_snap is None:
        return
    live: set[tuple[str, str, str]] = {(g.permission, g.holder_type, g.holder_parameter) for g in scheme_snap.grants}
    declared: set[tuple[str, str, str]] = set()
    for permission, holders in scheme.grants.items():
        for holder in holders:
            from stint.schema.permissions import (
                holder_jira_type,
            )
            from stint.schema.permissions import (
                holder_parameter as holder_param,
            )

            holder_type = holder_jira_type(holder)
            declared_value = holder_param(holder)
            try:
                resolved = await dialect.resolve_permission_holder(holder_type, declared_value)
            except ConfigurationError as exc:
                report.warnings.append(
                    f"permission scheme {scheme.alias!r}: cannot resolve {holder_type} holder {declared_value!r}: {exc}"
                )
                continue
            key = (permission, holder_type, resolved or "")
            declared.add(key)
            if key not in live:
                report.errors.append(
                    f"permission scheme {scheme.alias!r}: grant {permission} to {holder_type} "
                    f"{declared_value!r} is not in the live scheme ({_ROLE_LOCKOUT_NOTE})"
                )
    for permission, holder_type, holder_parameter in sorted(live - declared):
        if scheme.copy_from is None:
            report.warnings.append(
                f"permission scheme {scheme.alias!r}: grant {permission} to {holder_type} "
                f"{holder_parameter!r} is in the live scheme but not declared in the schema"
            )
        else:
            report.warnings.append(
                f"permission scheme {scheme.alias!r}: grant {permission} to {holder_type} "
                f"{holder_parameter!r} is inherited/unmanaged (copy_from={scheme.copy_from!r})"
            )


def _check_issuetype(
    it: type[IssueType],
    state: Any,
    snapshot: Any,
    report: CheckReport,
    scheme_members: set[str] | None,
    create_fields: dict[str, set[str]],
) -> None:
    """Compare one declared issuetype (and its fields) against the snapshot."""
    label = it.__name__
    alias = getattr(it, "__alias__", "")
    it_mapping = state.issuetypes.get(alias)
    if it_mapping is None:
        report.errors.append(
            f"{label}: issuetype {alias!r} is not in state; run `stint stamp` or `stint upgrade` first."
        )
        return
    it_snap = snapshot.issuetypes.get(it_mapping.id)
    if it_snap is None:
        report.errors.append(f"{label}: issuetype {alias!r} (id {it_mapping.id}) does not exist on the tenant")
    elif it_snap.name != it.__title__:
        report.warnings.append(
            f"{label}: issuetype renamed on the tenant: declared {it.__title__!r}, live {it_snap.name!r}"
        )
    if scheme_members is not None and it_mapping.id not in scheme_members:
        report.errors.append(
            f"{label}: issuetype {alias!r} (id {it_mapping.id}) is not on project {report.project}'s issue-type scheme"
        )

    meta_fields = create_fields.get(it_mapping.id)
    if meta_fields is None:
        report.errors.append(f"{label}: no create screen for this issuetype on project {report.project}")
        return

    for attr_name, field_info in it.model_fields.items():
        if attr_name == "key" or attr_name in _CREATE_EXEMPT:
            continue
        cf = next((m for m in field_info.metadata if isinstance(m, CustomField)), None)
        if cf is None:
            if attr_name not in meta_fields:
                report.errors.append(
                    f"{label}.{attr_name}: system field {attr_name!r} is not on the create screen for this issuetype"
                )
            continue

        mapping = state.custom_fields.get(cf.alias)
        if mapping is None:
            report.errors.append(
                f"{label}.{attr_name}: custom field {cf.alias!r} is not in state; "
                f"run `stint stamp` or `stint upgrade` first."
            )
            continue
        cf_snap = snapshot.custom_fields.get(mapping.id)
        if cf_snap is None:
            report.errors.append(
                f"{label}.{attr_name}: custom field {cf.alias!r} ({mapping.id}) does not exist on the tenant"
            )
            continue
        if cf.type in _SELECT_STYLES:
            declared = list(cf.options or [])
            live = set(cf_snap.options)
            missing = [o for o in declared if o not in live]
            if missing:
                report.errors.append(
                    f"{label}.{attr_name}: option(s) {missing} missing from field "
                    f"{cf.name!r} ({mapping.id}); live options: {sorted(live)}"
                )
            extra = sorted(live - set(declared))
            if extra:
                report.warnings.append(
                    f"{label}.{attr_name}: field {cf.name!r} has option(s) not declared in the schema: {extra}"
                )
        if cf.description and cf_snap.description and cf_snap.description != cf.description:
            report.warnings.append(f"{label}.{attr_name}: field {cf.name!r} description changed on the tenant")
        if mapping.id not in meta_fields:
            report.errors.append(
                f"{label}.{attr_name}: field {cf.alias!r} ({mapping.id}) is not on the create screen for this issuetype"
            )


async def _createmeta_fields(dialect: Any, project_key: str) -> dict[str, set[str]]:
    """issuetype id -> set of field keys settable on create.

    Read-only: one ``GET /issue/createmeta?projectKeys=...&expand=projects.issuetypes.fields``.
    """
    raw = await dialect.get_createmeta(project_key)
    out: dict[str, set[str]] = {}
    if not isinstance(raw, dict):
        return out
    for proj in raw.get("projects") or []:
        for it in proj.get("issuetypes") or []:
            fields = it.get("fields") or {}
            out[str(it.get("id", ""))] = set(fields.keys())
    return out
