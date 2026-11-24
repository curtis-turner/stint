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
