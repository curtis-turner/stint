"""Module-level registry of declared projects, issuetypes, custom fields, screens, and security schemes.

Populated by class definition (metaclass for IssueType / Project) and by direct
instantiation (CustomField, Screen, ScreenScheme, FieldConfiguration,
SecurityScheme).

For M0 this is a single process-wide singleton. Later milestones may need a
per-engine registry once multi-tenant setups are real.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from stint.exceptions import RegistryError

if TYPE_CHECKING:
    from stint.fields import CustomField
    from stint.schema.field_config import FieldConfiguration
    from stint.schema.issuetype import IssueType
    from stint.schema.project import Project
    from stint.schema.screen import Screen, ScreenScheme
    from stint.schema.security import SecurityScheme


def _check_replace_on_reload(
    *,
    kind: str,
    alias: str,
    known: object,
    incoming_module: str,
) -> None:
    """Reload semantics for duplicate registration (L18).

    Re-executing a schema module (file path loading re-execs; dot-imported
    modules can be re-imported) re-declares its own objects: the alias is
    already taken by *the same declaring module*, and the new objects must
    replace the old ones instead of raising ``Duplicate ... alias``. Only a
    declaration of the same alias from a *different* module is a real
    duplicate and keeps raising.
    """
    known_module = getattr(known, "_declared_module", None)
    if known_module == incoming_module:
        return
    raise RegistryError(
        f"Duplicate {kind} alias {alias!r}"
        + (f" (already declared by module {known_module!r})" if known_module else "")
    )


def _declaring_module() -> str:
    """Best-effort dotted name of the module that created the declared object.

    Registration is invoked from ``__post_init__`` hooks inside stint, so
    walk outward to the first frame that is not stint-internal. Frames from
    synthetic loader names (``stint_schema_<stem>``) count as user schema
    code, as does ``__main__`` (inline declarations in a script). Used by
    scoped autogenerate to attribute declared-but-unreferenced objects to
    their defining module (M5).
    """
    for frame_info in inspect.stack():
        name = frame_info.frame.f_globals.get("__name__", "")
        if not name:
            continue
        if name == "stint" or name.startswith("stint."):
            continue
        if name == "dataclasses":
            # dataclasses' generated __init__ runs user __post_init__ from
            # inside the dataclasses module globals — skip past it.
            continue
        return name
    return ""


class Registry:
    """Tracks declared schema objects by alias and by Python class."""

    def __init__(self) -> None:
        self.issuetypes: dict[str, type[IssueType]] = {}
        self.projects: dict[str, type[Project]] = {}
        self.custom_fields: dict[str, CustomField] = {}
        self.screens: dict[str, Screen] = {}
        self.screen_schemes: dict[str, ScreenScheme] = {}
        self.field_configurations: dict[str, FieldConfiguration] = {}
        self.security_schemes: dict[str, SecurityScheme] = {}

    def register_issuetype(self, cls: type[IssueType]) -> None:
        alias = getattr(cls, "__alias__", None)
        if not alias:
            raise RegistryError(f"IssueType {cls.__name__} is missing __alias__")
        known = self.issuetypes.get(alias)
        if known is not None and known is not cls:
            _check_replace_on_reload(kind="IssueType", alias=alias, known=known, incoming_module=cls.__module__)
        self.issuetypes[alias] = cls
        # Declaring module for reload semantics / scoping (L18): classes get
        # it from the import machinery, instances from frame inspection.
        cls._declared_module = cls.__module__  # ty: ignore[unresolved-attribute]

    def register_project(self, cls: type[Project]) -> None:
        key = getattr(cls, "__key__", None)
        if not key:
            raise RegistryError(f"Project {cls.__name__} is missing __key__")
        known = self.projects.get(key)
        if known is not None and known is not cls:
            _check_replace_on_reload(kind="Project", alias=key, known=known, incoming_module=cls.__module__)
        self.projects[key] = cls
        # Declaring module for reload semantics / scoping (L18): classes get
        # it from the import machinery, instances from frame inspection.
        cls._declared_module = cls.__module__  # ty: ignore[unresolved-attribute]

    def register_custom_field(self, field: CustomField) -> None:
        module = _declaring_module()
        known = self.custom_fields.get(field.alias)
        if known is not None and known is not field:
            _check_replace_on_reload(kind="CustomField", alias=field.alias, known=known, incoming_module=module)
        self.custom_fields[field.alias] = field
        field._declared_module = module

    def register_screen(self, screen: Screen) -> None:
        module = _declaring_module()
        known = self.screens.get(screen.alias)
        if known is not None and known is not screen:
            _check_replace_on_reload(kind="Screen", alias=screen.alias, known=known, incoming_module=module)
        self.screens[screen.alias] = screen
        screen._declared_module = module

    def register_screen_scheme(self, scheme: ScreenScheme) -> None:
        module = _declaring_module()
        known = self.screen_schemes.get(scheme.alias)
        if known is not None and known is not scheme:
            _check_replace_on_reload(kind="ScreenScheme", alias=scheme.alias, known=known, incoming_module=module)
        self.screen_schemes[scheme.alias] = scheme
        scheme._declared_module = module

    def register_field_configuration(self, fc: FieldConfiguration) -> None:
        module = _declaring_module()
        known = self.field_configurations.get(fc.alias)
        if known is not None and known is not fc:
            _check_replace_on_reload(kind="FieldConfiguration", alias=fc.alias, known=known, incoming_module=module)
        self.field_configurations[fc.alias] = fc
        fc._declared_module = module

    def register_security_scheme(self, scheme: SecurityScheme) -> None:
        """Register a schema-declared ``SecurityScheme`` (from its
        ``__post_init__``; reflection-built snapshots never touch the registry)."""
        alias = getattr(scheme, "alias", "")
        if not alias:
            raise RegistryError("SecurityScheme declaration requires a non-empty alias")
        module = _declaring_module()
        known = self.security_schemes.get(alias)
        if known is not None and known is not scheme:
            _check_replace_on_reload(kind="SecurityScheme", alias=alias, known=known, incoming_module=module)
        self.security_schemes[alias] = scheme
        scheme._declared_module = module  # ty: ignore[unresolved-attribute]

    def reset(self) -> None:
        """Used by tests to isolate state."""
        self.issuetypes.clear()
        self.projects.clear()
        self.custom_fields.clear()
        self.screens.clear()
        self.screen_schemes.clear()
        self.field_configurations.clear()
        self.security_schemes.clear()


registry = Registry()
