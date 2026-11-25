"""Lockout guard: never remove the owning account's own access.

Every destructive change to role membership or permission-scheme grants can
remove the account stint authenticates as from the project it manages. The
guard is a pure evaluation over *post-change* holder data:

- inputs: the grants that would survive the plan, the account's facts
  (accountId, project-role memberships, group names) and the project lead;
- output: which required permissions the account would no longer hold.

Callers fetch the live facts from the dialect and simulate the change before
calling ``missing_permissions``. A permission is "held" when some surviving
grant's holder matches the account:

- ``projectRole`` holder — the role id resolves to a role name the account
  is an actor of;
- ``group`` holder — the group is one of the account's groups;
- ``user`` holder — the parameter is the account's own accountId;
- ``applicationRole`` / ``anyone`` — any logged-in user holds it (removing
  a role or grant cannot take this away);
- ``projectLead`` — the account is the project's lead;
- ``reporter`` / ``assignee`` — per-issue holders that cannot be verified at
  plan time; they never count as evidence of retention (conservative: a
  false refusal with a clear error beats a silent lockout).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Role-actor removals: the account must stay able to see and administer the
# project.
DEFAULT_ROLE_GUARD_PERMISSIONS: tuple[str, ...] = ("BROWSE_PROJECTS", "ADMINISTER_PROJECTS")

# Permission-scheme grant changes: a service account driving issues through
# stint must keep creating, editing, and transitioning them.
DEFAULT_SCHEME_GUARD_PERMISSIONS: tuple[str, ...] = (
    "BROWSE_PROJECTS",
    "ADMINISTER_PROJECTS",
    "EDIT_ISSUES",
    "TRANSITION_ISSUES",
    "CREATE_ISSUES",
)


def holder_grants_permission(
    holder_type: str,
    holder_parameter: str,
    *,
    account_id: str,
    account_role_names: Iterable[str],
    account_groups: Iterable[str],
    project_lead_id: str | None,
    role_name_to_id: Mapping[str, str],
) -> bool:
    """Whether one grant holder matches the authenticated account."""
    holder_type = holder_type.lower()
    if holder_type in ("applicationrole", "anyone"):
        return True
    if holder_type == "user":
        return bool(holder_parameter) and holder_parameter == account_id
    if holder_type == "projectrole":
        # Jira's holder parameter is the role id; account facts are names.
        role_names = {name for name, rid in role_name_to_id.items() if rid == holder_parameter}
        return bool(role_names & set(account_role_names))
    if holder_type == "group":
        return bool(holder_parameter) and holder_parameter in set(account_groups)
    if holder_type == "projectlead":
        return bool(project_lead_id) and project_lead_id == account_id
    # reporter / assignee / userCustomField / groupCustomField / unknown:
    # per-issue or unresolvable — never count as retention evidence.
    return False


# ── Transition restrict guard ─────────────────────────────────────────


def restrict_rule_parameters(conditions: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Extract the ``system:restrict-issue-transition`` parameters from a
    reflected transition's ``conditions`` (a ``ConditionGroupConfiguration``
    that may nest ``conditionGroups`` recursively).

    Returns the aggregate parameters map (``roleIds``/``groupIds``/
    ``permissionKeys``/``accountIds``/... — comma-separated strings), or
    None when the transition is unrestricted. Multiple restrict rules in
    one conditions tree are merged (union of each parameter key); the rule
    evaluates as true when *any* criterion is met, so unioning the
    holders keeps the evaluation conservative for "is the account among
    them".
    """
    if not isinstance(conditions, Mapping):
        return None
    params: dict[str, str] = {}

    def _collect(group: Mapping[str, Any]) -> None:
        for condition in group.get("conditions") or ():
            if not isinstance(condition, Mapping):
                continue
            rule_key = str(condition.get("ruleKey", ""))
            if rule_key == "system:restrict-issue-transition":
                for key, value in (condition.get("parameters") or {}).items():
                    existing = params.get(key, "")
                    merged = {v.strip() for v in f"{existing},{value}".split(",") if v.strip()}
                    params[key] = ",".join(sorted(merged))
            elif rule_key == "system:restrict-from-all-users":
                # Blocks everyone (optionally API clients too); treat as an
                # empty accountIds allow-list: nobody qualifies.
                params.setdefault("accountIds", "")
        for sub in group.get("conditionGroups") or ():
            if isinstance(sub, Mapping):
                _collect(sub)

    _collect(conditions)
    return params or None


def account_can_transition(
    params: Mapping[str, str] | None,
    *,
    account_id: str,
    account_role_ids: Iterable[str],
    account_group_ids: Iterable[str],
    held_permission_keys: Iterable[str] = (),
) -> bool:
    """Whether the account can use a transition restricted by ``params``.

    The restrict rule evaluates true when ANY criterion is met
    (roleIds / groupIds / permissionKeys / accountIds). ``params=None``
    (unrestricted) → always usable. Without ``held_permission_keys`` a
    permission-only restriction is unprovable (returns False) — the
    conservative direction: usability is never assumed."""
    if params is None:
        return True
    role_ids = set(account_role_ids)
    group_ids = set(account_group_ids)
    held_permission_keys = frozenset(held_permission_keys or ())
    allowed_accounts = {v.strip() for v in str(params.get("accountIds", "")).split(",") if v.strip()}
    if account_id in allowed_accounts:
        return True
    allowed_roles = {v.strip() for v in str(params.get("roleIds", "")).split(",") if v.strip()}
    if role_ids & allowed_roles:
        return True
    allowed_groups = {v.strip() for v in str(params.get("groupIds", "")).split(",") if v.strip()}
    if group_ids & allowed_groups:
        return True
    allowed_permissions = {v.strip() for v in str(params.get("permissionKeys", "")).split(",") if v.strip()}
    if allowed_permissions & held_permission_keys:
        return True
    return False


def lost_transitions(
    before: Mapping[str, Mapping[str, str] | None],
    after: Mapping[str, Mapping[str, str] | None],
    *,
    account_id: str,
    account_role_ids: Iterable[str],
    account_group_ids: Iterable[str],
    held_permission_keys: Iterable[str] = (),
) -> tuple[str, ...]:
    """Transitions the account provably loses between the old and new state.

    Keys are transition identities (``"<workflow>:<transition name>"``);
    values are the restrict parameters (None = unrestricted). A
    transition is "lost" when the account provably could use it before and
    provably cannot after. Transitions absent from ``after`` count as
    lost too (deleted transitions the account used). Restrictions whose
    holders cannot be verified (roles without project context,
    permissions without ``held_permission_keys``) are never provable and
    never produce a violation by themselves.

    Returns the offending transition keys (empty = no lockout).
    """
    role_ids = set(account_role_ids)
    group_ids = set(account_group_ids)
    permissions = frozenset(held_permission_keys or ())
    lost: list[str] = []
    for key, old_params in before.items():
        if not account_can_transition(
            old_params,
            account_id=account_id,
            account_role_ids=role_ids,
            account_group_ids=group_ids,
            held_permission_keys=permissions,
        ):
            continue  # not provably usable today → no violation
        if key not in after:
            lost.append(key)  # the transition is being deleted
            continue
        if account_can_transition(
            after[key],
            account_id=account_id,
            account_role_ids=role_ids,
            account_group_ids=group_ids,
            held_permission_keys=permissions,
        ):
            continue
        lost.append(key)
    return tuple(lost)


def missing_permissions(
    required: Iterable[str],
    grants: Mapping[str, Iterable[tuple[str, str]]],
    *,
    account_id: str,
    account_role_names: Iterable[str],
    account_groups: Iterable[str],
    project_lead_id: str | None,
    role_name_to_id: Mapping[str, str],
) -> list[str]:
    """Which of ``required`` the account would NOT hold after the change.

    ``grants`` maps permission key -> surviving (holder_type,
    holder_parameter) pairs. Order follows ``required``.
    """
    roles = set(account_role_names)
    groups = set(account_groups)
    missing: list[str] = []
    for permission in required:
        holders = grants.get(permission) or ()
        if any(
            holder_grants_permission(
                htype,
                hparam,
                account_id=account_id,
                account_role_names=roles,
                account_groups=groups,
                project_lead_id=project_lead_id,
                role_name_to_id=role_name_to_id,
            )
            for htype, hparam in holders
        ):
            continue
        missing.append(permission)
    return missing
