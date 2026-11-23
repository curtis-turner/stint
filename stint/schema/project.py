"""Project base. Carries deployment-mode (CMP vs TMP) validation."""

from __future__ import annotations

from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel

from stint.exceptions import ConfigurationError
from stint.registry import registry
from stint.schema._meta import StintMeta

ProjectStyle = Literal["company-managed", "team-managed"]

_ALLOWED_STYLES: tuple[str, ...] = ("company-managed", "team-managed")


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
    # Declared so type-checkers see the key subclasses carry (validated by
    # ProjectMeta). __issuetypes__ is read via getattr() like validate.py,
    # since schemas declare it as a list and ProjectMeta normalizes it.
    __key__: ClassVar[str]

    _ALLOWED_STYLES: ClassVar[tuple[str, ...]] = _ALLOWED_STYLES
