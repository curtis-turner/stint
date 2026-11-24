"""Schema-plane declarations for permission schemes (``PermissionScheme``).

``PermissionScheme`` is a declaration dataclass registered with the
``Registry`` when constructed inside user schema code (same lifecycle as
``SecurityScheme``). A project binds it with ``Project.__permission_scheme__``.

Holder types a grant list accepts:

- ``ProjectRole("Name")`` — members of a project role (managed with
  ``Project.__roles__``).
- ``Group("name")`` / ``User("email-or-accountId")`` — shared value types
  from ``stint.schema.roles``.
- ``ApplicationRole("jira-software")`` — users with access to an
  application; ``ApplicationRole()`` with no name means any logged-in user.
- ``Reporter()``, ``Assignee()``, ``ProjectLead()`` — dynamic per-issue
  holders with no parameter.

Grant policy: declared grants are always managed (added when missing).
Live grants that are *not* declared are removed only when the scheme has no
``copy_from`` (strict/exact policy, gated behind ``--allow-delete``); with
``copy_from`` they are left as-is and reported by ``engine.check``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stint.exceptions import RegistryError
from stint.registry import registry
from stint.schema.roles import Group, User

__all__ = [
    "Assignee",
    "ApplicationRole",
    "KNOWN_PERMISSION_KEYS",
    "PermissionScheme",
    "ProjectLead",
    "ProjectRole",
    "Reporter",
]


# The built-in Jira permission keys a permission scheme can grant
# (https://developer.atlassian.com/cloud/jira/platform/rest/v3/ —
# "Get all permission schemes" > Built-in permissions). validate() rejects
# grant keys outside this set; app-defined custom permissions are out of
# scope for now.
KNOWN_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        # Administration
        "ADMINISTER_PROJECTS",
        "EDIT_WORKFLOW",
        "EDIT_ISSUE_LAYOUT",
        # Project
        "BROWSE_PROJECTS",
        "MANAGE_SPRINTS_PERMISSION",
        "SERVICEDESK_AGENT",
        "VIEW_DEV_TOOLS",
        "VIEW_READONLY_WORKFLOW",
        # Issue
        "ASSIGNABLE_USER",
        "ASSIGN_ISSUES",
        "CLOSE_ISSUES",
        "CREATE_ISSUES",
        "DELETE_ISSUES",
        "EDIT_ISSUES",
        "LINK_ISSUES",
        "MODIFY_REPORTER",
        "MOVE_ISSUES",
        "RESOLVE_ISSUES",
        "SCHEDULE_ISSUES",
        "SET_ISSUE_SECURITY",
        "TRANSITION_ISSUES",
        # Voters and watchers
        "MANAGE_WATCHERS",
        "VIEW_VOTERS_AND_WATCHERS",
        # Comments
        "ADD_COMMENTS",
        "DELETE_ALL_COMMENTS",
        "DELETE_OWN_COMMENTS",
        "EDIT_ALL_COMMENTS",
        "EDIT_OWN_COMMENTS",
        # Attachments
        "CREATE_ATTACHMENTS",
        "DELETE_ALL_ATTACHMENTS",
        "DELETE_OWN_ATTACHMENTS",
        # Time tracking
        "DELETE_ALL_WORKLOGS",
        "DELETE_OWN_WORKLOGS",
        "EDIT_ALL_WORKLOGS",
        "EDIT_OWN_WORKLOGS",
        "WORK_ON_ISSUES",
    }
)

# Holder types with no ``parameter`` (dynamic, per-issue or all-user grants).
_PARAMETERLESS_HOLDER_TYPES: frozenset[str] = frozenset({"reporter", "assignee", "projectLead"})


@dataclass(frozen=True)
class ProjectRole:
    """Grant holder: members of the named project role.

    Declared by role *name*; resolved to Jira's role id (the ``holder``
    parameter the permission-scheme API uses) at apply time.
    """

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ProjectRole requires a non-empty role name")


@dataclass(frozen=True)
class ApplicationRole:
    """Grant holder: users with access to an application (any logged-in
    user when no application name is given)."""

    name: str = ""

    def __post_init__(self) -> None:
        pass  # parameterless form is valid


@dataclass(frozen=True)
class Reporter:
    """Grant holder: the user who reported the issue."""

    def __post_init__(self) -> None:
        pass


@dataclass(frozen=True)
class Assignee:
    """Grant holder: the user currently assigned to the issue."""

    def __post_init__(self) -> None:
        pass


@dataclass(frozen=True)
class ProjectLead:
    """Grant holder: the project lead."""

    def __post_init__(self) -> None:
        pass


PermissionGrantHolder = ProjectRole | Group | User | ApplicationRole | Reporter | Assignee | ProjectLead


def holder_jira_type(holder: PermissionGrantHolder) -> str:
    """Map a declared holder to Jira's ``holder.type`` string."""
    if isinstance(holder, ProjectRole):
        return "projectRole"
    if isinstance(holder, Group):
        return "group"
    if isinstance(holder, User):
        return "user"
    if isinstance(holder, ApplicationRole):
        return "applicationRole"
    if isinstance(holder, Reporter):
        return "reporter"
    if isinstance(holder, Assignee):
        return "assignee"
    if isinstance(holder, ProjectLead):
        return "projectLead"
    raise RegistryError(f"Unsupported permission grant holder {holder!r}")


def holder_parameter(holder: PermissionGrantHolder) -> str:
    """The declared ``holder.parameter`` value ("" for parameterless types).

    ProjectRole holders carry the role *name* here; ops resolve it to Jira's
    role id at apply time. User holders carry an email or raw accountId.
    """
    if isinstance(holder, ProjectRole):
        return holder.name
    if isinstance(holder, Group):
        return holder.name
    if isinstance(holder, User):
        return holder.value
    if isinstance(holder, ApplicationRole):
        return holder.name
    if isinstance(holder, (Reporter, Assignee, ProjectLead)):
        return ""
    raise RegistryError(f"Unsupported permission grant holder {holder!r}")


def _holder_type_name(holder: object) -> str:
    return type(holder).__name__


@dataclass
class PermissionScheme:
    """A Jira permission scheme plus its grants, bound to a ``Project``.

    Registering fields:

    - ``alias``: stable identifier across tenants (state-file key).
    - ``name``: Jira display name; defaults to the alias.
    - ``grants``: permission key → list of holders. Keys must be built-in
      Jira permission keys (see ``KNOWN_PERMISSION_KEYS``); unknown keys
      raise at declaration time.
    - ``copy_from``: optional *name* of an existing Jira permission scheme
      whose grants seed a newly created scheme. Declared grants are then
      added on top; grants inherited from the source scheme are NOT managed
      (they stay even when undeclared, and ``engine.check`` reports them as
      unmanaged). Without ``copy_from`` the scheme is exactly what is
      declared: undeclared live grants are drift and get removed with
      ``--allow-delete``.

    The binding is ``Project.__permission_scheme__`` (authoritative);
    ``project`` is an optional project key hint like ``SecurityScheme``'s.
    """

    alias: str
    name: str = ""
    description: str = ""
    copy_from: str | None = None
    grants: dict[str, list[PermissionGrantHolder]] = field(default_factory=dict)
    project: str | None = None  # project key hint (non-authoritative)

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("PermissionScheme declaration requires a non-empty alias")
        if not self.name:
            self.name = self.alias
        if self.copy_from is not None and not self.copy_from.strip():
            raise RegistryError(f"PermissionScheme {self.alias!r}: copy_from must name an existing scheme")
        for permission, holders in self.grants.items():
            if permission not in KNOWN_PERMISSION_KEYS:
                raise RegistryError(
                    f"PermissionScheme {self.alias!r}: unknown permission key {permission!r}. "
                    f"Use a built-in Jira permission key, e.g. one of: "
                    f"{', '.join(sorted(KNOWN_PERMISSION_KEYS))}."
                )
            if not holders:
                raise RegistryError(
                    f"PermissionScheme {self.alias!r}: permission {permission!r} has an empty holder list"
                )
            for holder in holders:
                jira_type = holder_jira_type(holder)
                parameter = holder_parameter(holder)
                if jira_type not in _PARAMETERLESS_HOLDER_TYPES and not parameter:
                    raise RegistryError(
                        f"PermissionScheme {self.alias!r}: permission {permission!r} has a "
                        f"{_holder_type_name(holder)} holder with no value"
                    )
        registry.register_permission_scheme(self)

    def declared_grants(self) -> list[tuple[str, str, str]]:
        """(permission, jira holder type, declared parameter) triples in
        declaration order. The declared parameter is the *schema-plane*
        value: role name for projectRole holders, email for user holders —
        ops resolve both at apply time."""
        out: list[tuple[str, str, str]] = []
        for permission, holders in self.grants.items():
            for holder in holders:
                out.append((permission, holder_jira_type(holder), holder_parameter(holder)))
        return out
