"""Project base. Carries deployment-mode (CMP vs TMP) validation."""

from __future__ import annotations

from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel

from stint.exceptions import ConfigurationError
from stint.registry import registry
from stint.schema._meta import StintMeta

ProjectStyle = Literal["company-managed", "team-managed"]

_ALLOWED_STYLES: tuple[str, ...] = ("company-managed", "team-managed")


def _validate_roles(project_name: str, style: str, roles: Any, lead_role: str | None) -> None:
    """Validate a ``Project.__roles__`` declaration at class-definition time."""
    from stint.schema.roles import Group, User

    if not isinstance(roles, dict) or not roles:
        raise ConfigurationError(
            f"Project {project_name!r} has an invalid __roles__ {roles!r}; "
            f"use a non-empty mapping of role name -> list of User/Group actors."
        )
    for role_name, actors in roles.items():
        if not isinstance(role_name, str) or not role_name.strip():
            raise ConfigurationError(
                f"Project {project_name!r} has an invalid role name {role_name!r} in __roles__; "
                f"use the name of an existing Jira project role."
            )
        if not isinstance(actors, (list, tuple)) or not actors:
            raise ConfigurationError(
                f"Project {project_name!r}: role {role_name!r} in __roles__ must carry a "
                f"non-empty list of User/Group actors."
            )
        for actor in actors:
            if not isinstance(actor, (User, Group)):
                raise ConfigurationError(
                    f"Project {project_name!r}: role {role_name!r} in __roles__ has an "
                    f"invalid actor {actor!r}; use User('email') or Group('name')."
                )
    if lead_role is not None and lead_role in roles:
        raise ConfigurationError(
            f"Project {project_name!r} declares __lead_role__ {lead_role!r} which is also "
            f"declared in __roles__; declare the lead as a User actor in __roles__ "
            f"instead (the two spellings of the same role conflict)."
        )
    if style == "team-managed":
        raise ConfigurationError(
            f"Project {project_name!r} is team-managed but declares __roles__; "
            f"project-role actors are a company-managed scheme surface."
        )


class ProjectMeta(StintMeta):
    """Validates and registers Project subclasses after dunders are reattached."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> type[Project]:
        cls = cast("type[Project]", super().__new__(mcs, name, bases, namespace, **kwargs))
        if name == "Project":
            return cls

        key = getattr(cls, "__key__", None)
        if not key:
            raise ConfigurationError(f"Project {name!r} is missing __key__")

        if getattr(cls, "__title__", None) is None:
            cls.__title__ = name

        style = getattr(cls, "__style__", "company-managed")
        if style not in _ALLOWED_STYLES:
            raise ConfigurationError(
                f"Project {name!r} has invalid __style__ {style!r}; expected one of {_ALLOWED_STYLES}"
            )

        issuetypes = getattr(cls, "__issuetypes__", None)
        if not issuetypes:
            raise ConfigurationError(f"Project {name!r} is missing __issuetypes__")

        if style == "team-managed":
            for it in issuetypes:
                if getattr(it, "__screen_scheme__", None) is not None:
                    raise ConfigurationError(
                        f"Project {name!r} is team-managed but its issuetype "
                        f"{it.__name__!r} references a ScreenScheme. "
                        f"Team-managed projects do not support reusable screen "
                        f"schemes. Use per-issuetype screen field lists instead."
                    )
                if getattr(it, "__field_configuration__", None) is not None:
                    raise ConfigurationError(
                        f"Project {name!r} is team-managed but its issuetype "
                        f"{it.__name__!r} references a FieldConfiguration. "
                        f"Team-managed projects do not support reusable field "
                        f"configurations."
                    )
            if getattr(cls, "__security_scheme__", None) is not None:
                raise ConfigurationError(
                    f"Project {name!r} is team-managed but references a SecurityScheme. "
                    f"Team-managed projects have no project-level issue security "
                    f"scheme association."
                )

        security_scheme = getattr(cls, "__security_scheme__", None)
        if security_scheme is not None and getattr(security_scheme, "alias", "") == "":
            raise ConfigurationError(
                f"Project {name!r} references a SecurityScheme with no alias; give the declaration an explicit alias."
            )

        lead_role = getattr(cls, "__lead_role__", None)
        if lead_role is not None:
            if not isinstance(lead_role, str) or not lead_role.strip():
                raise ConfigurationError(
                    f"Project {name!r} has an invalid __lead_role__ {lead_role!r}; "
                    f"use the name of an existing Jira project role, or omit it."
                )
            if style == "team-managed":
                raise ConfigurationError(
                    f"Project {name!r} is team-managed but declares __lead_role__; "
                    f"project-role actors are a company-managed scheme surface."
                )

        roles = getattr(cls, "__roles__", None)
        if roles is not None:
            _validate_roles(name, style, roles, lead_role)

        permission_scheme = getattr(cls, "__permission_scheme__", None)
        if permission_scheme is not None:
            if style == "team-managed":
                raise ConfigurationError(
                    f"Project {name!r} is team-managed but references a PermissionScheme. "
                    f"Team-managed projects have no project-level permission "
                    f"scheme association."
                )
            if getattr(permission_scheme, "alias", "") == "":
                raise ConfigurationError(
                    f"Project {name!r} references a PermissionScheme with no alias; "
                    f"give the declaration an explicit alias."
                )

        workflow_scheme = getattr(cls, "__workflow_scheme__", None)
        if workflow_scheme is not None:
            if style == "team-managed":
                raise ConfigurationError(
                    f"Project {name!r} is team-managed but references a WorkflowScheme. "
                    f"Team-managed projects have no shared workflow scheme "
                    f"association."
                )
            if getattr(workflow_scheme, "alias", "") == "":
                raise ConfigurationError(
                    f"Project {name!r} references a WorkflowScheme with no alias; "
                    f"give the declaration an explicit alias."
                )
            declared_aliases = {it_alias for it in issuetypes if (it_alias := getattr(it, "__alias__", ""))}
            for it_alias in workflow_scheme.mappings or {}:
                if it_alias not in declared_aliases:
                    raise ConfigurationError(
                        f"Project {name!r} WorkflowScheme mapping key {it_alias!r} is not "
                        f"one of the project's declared issuetype aliases "
                        f"({sorted(declared_aliases)})."
                    )

        # Track inverse linkage: each issuetype learns which project(s) include
        # it. Used by AsyncSession.add() to infer project when unambiguous.
        for it in issuetypes:
            it.__projects__ = tuple(getattr(it, "__projects__", ())) + (key,)

        registry.register_project(cls)
        return cls


class Project(BaseModel, metaclass=ProjectMeta):
    """Base class for projects.

    Subclasses declare:

    - ``__key__``: Jira project key (e.g. ``"PLAT"``). Stable identifier.
    - ``__title__``: optional Jira display name. Defaults to the class name.
    - ``__lead__``: optional project lead email. stint resolves it to the
      Jira Cloud accountId at apply time via the user-search API. A raw
      accountId (no ``@``) is passed through unchanged. Resolution needs the
      "Browse users and groups" permission.
    - ``__lead_role__``: optional name of an existing Jira project role
      (e.g. ``"Browse"``) the lead is added to at apply time. ``POST
      /project`` does not grant the lead any role, and tenants whose
      permission scheme grants ``BROWSE_PROJECTS`` only through project
      roles leave the lead unable to see the project they own (adding the
      lead to a role is what Jira's own UI flow does). CMP only.
    - ``__roles__``: optional mapping of project role name -> list of
      ``User``/``Group`` actors. stint reflects each declared role's live
      actors and diffs membership into add/remove actor ops; roles that
      are not declared are left unmanaged. Declaring the lead's role here
      (with the lead as a ``User`` actor) is the general form of
      ``__lead_role__``. CMP only.
    - ``__permission_scheme__``: optional PermissionScheme declaration to
      create/keep in sync and associate with this project at apply time.
      CMP only.
    - ``__style__``: ``"company-managed"`` (default) or ``"team-managed"`` (Cloud only).
    - ``__issuetypes__``: list of IssueType subclasses included in this project.
    - ``__security_scheme__``: optional SecurityScheme declaration to
      associate with this project at apply time. CMP only.

    TMP projects cannot reference reusable schemes (ScreenScheme,
    FieldConfiguration, SecurityScheme); stint rejects this combination at
    class-definition time.
    """

    # Set by ProjectMeta after class creation; declared for the type checker.
    __title__: ClassVar[str]
    __lead_role__: ClassVar[str | None] = None
    __roles__: ClassVar[dict[str, list[Any]] | None] = None
    __permission_scheme__: ClassVar[Any] = None
    __workflow_scheme__: ClassVar[Any] = None
    # Declared so type-checkers see the key subclasses carry (validated by
    # ProjectMeta). __issuetypes__ is read via getattr() like validate.py,
    # since schemas declare it as a list and ProjectMeta normalizes it.
    __key__: ClassVar[str]

    _ALLOWED_STYLES: ClassVar[tuple[str, ...]] = _ALLOWED_STYLES
