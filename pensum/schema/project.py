"""Project base. Carries deployment-mode (CMP vs TMP) validation."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from pensum.exceptions import ConfigurationError
from pensum.registry import registry
from pensum.schema._meta import PensumMeta

ProjectStyle = Literal["company-managed", "team-managed"]

_ALLOWED_STYLES: tuple[str, ...] = ("company-managed", "team-managed")


class ProjectMeta(PensumMeta):
    """Validates and registers Project subclasses after dunders are reattached."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
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
    - ``__lead__``: optional project lead account ID / username.
    - ``__style__``: ``"company-managed"`` (default) or ``"team-managed"`` (Cloud only).
    - ``__issuetypes__``: list of IssueType subclasses included in this project.

    On DC, ``__style__`` is ignored at apply time (DC is CMP-equivalent). On
    Cloud, TMP projects cannot reference reusable schemes (ScreenScheme,
    FieldConfiguration); pensum rejects this combination at class-definition time.
    """

    _ALLOWED_STYLES: ClassVar[tuple[str, ...]] = _ALLOWED_STYLES
