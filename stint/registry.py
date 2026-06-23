"""Module-level registry of declared projects, issuetypes, custom fields, and screens.

Populated by class definition (metaclass for IssueType / Project) and by direct
instantiation (CustomField, Screen, ScreenScheme, FieldConfiguration).

For M0 this is a single process-wide singleton. Later milestones may need a
per-engine registry once multi-tenant setups are real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stint.exceptions import RegistryError

if TYPE_CHECKING:
    from stint.fields import CustomField
    from stint.schema.field_config import FieldConfiguration
    from stint.schema.issuetype import IssueType
    from stint.schema.project import Project
    from stint.schema.screen import Screen, ScreenScheme


class Registry:
    """Tracks declared schema objects by alias and by Python class."""

    def __init__(self) -> None:
        self.issuetypes: dict[str, type[IssueType]] = {}
        self.projects: dict[str, type[Project]] = {}
        self.custom_fields: dict[str, CustomField] = {}
        self.screens: dict[str, Screen] = {}
        self.screen_schemes: dict[str, ScreenScheme] = {}
        self.field_configurations: dict[str, FieldConfiguration] = {}

    def register_issuetype(self, cls: type[IssueType]) -> None:
        alias = getattr(cls, "__alias__", None)
        if not alias:
            raise RegistryError(f"IssueType {cls.__name__} is missing __alias__")
        if alias in self.issuetypes and self.issuetypes[alias] is not cls:
            raise RegistryError(f"Duplicate IssueType alias {alias!r}")
        self.issuetypes[alias] = cls

    def register_project(self, cls: type[Project]) -> None:
        key = getattr(cls, "__key__", None)
        if not key:
            raise RegistryError(f"Project {cls.__name__} is missing __key__")
        if key in self.projects and self.projects[key] is not cls:
            raise RegistryError(f"Duplicate Project key {key!r}")
        self.projects[key] = cls

    def register_custom_field(self, field: CustomField) -> None:
        if field.alias in self.custom_fields and self.custom_fields[field.alias] is not field:
            raise RegistryError(f"Duplicate CustomField alias {field.alias!r}")
        self.custom_fields[field.alias] = field

    def register_screen(self, screen: Screen) -> None:
        if screen.alias in self.screens and self.screens[screen.alias] is not screen:
            raise RegistryError(f"Duplicate Screen alias {screen.alias!r}")
        self.screens[screen.alias] = screen

    def register_screen_scheme(self, scheme: ScreenScheme) -> None:
        if scheme.alias in self.screen_schemes and self.screen_schemes[scheme.alias] is not scheme:
            raise RegistryError(f"Duplicate ScreenScheme alias {scheme.alias!r}")
        self.screen_schemes[scheme.alias] = scheme

    def register_field_configuration(self, fc: FieldConfiguration) -> None:
        if fc.alias in self.field_configurations and self.field_configurations[fc.alias] is not fc:
            raise RegistryError(f"Duplicate FieldConfiguration alias {fc.alias!r}")
        self.field_configurations[fc.alias] = fc

    def reset(self) -> None:
        """Used by tests to isolate state."""
        self.issuetypes.clear()
        self.projects.clear()
        self.custom_fields.clear()
        self.screens.clear()
        self.screen_schemes.clear()
        self.field_configurations.clear()


registry = Registry()
