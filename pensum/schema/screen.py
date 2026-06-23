"""Screen and ScreenScheme declarations (company-managed only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pensum.fields import CustomField

ScreenFieldRef = str | CustomField


@dataclass
class Screen:
    """A single Jira screen: an ordered list of system fields and custom fields.

    Strings are system field names (``"Summary"``, ``"Description"``, etc.).
    CustomField instances are the declared custom fields from ``pensum.fields``.
    """

    alias: str
    name: str
    fields: list[ScreenFieldRef] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.alias:
            raise ValueError("Screen requires a non-empty alias")
        if not self.name:
            raise ValueError("Screen requires a non-empty name")
        from pensum.registry import registry

        registry.register_screen(self)


@dataclass
class ScreenScheme:
    """A Jira screen scheme: maps Create/Edit/View operations to screens."""

    alias: str
    name: str
    create: Screen
    edit: Screen
    view: Screen
    description: str = ""

    def __post_init__(self) -> None:
        if not self.alias:
            raise ValueError("ScreenScheme requires a non-empty alias")
        if not self.name:
            raise ValueError("ScreenScheme requires a non-empty name")
        from pensum.registry import registry

        registry.register_screen_scheme(self)
