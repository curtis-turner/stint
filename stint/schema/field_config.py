"""FieldConfiguration declarations (company-managed only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from stint.fields import CustomField

FieldRef = str | CustomField


@dataclass
class FieldConfiguration:
    """Per-field required/hidden settings used across one or more issuetypes."""

    alias: str
    name: str
    required: list[FieldRef] = field(default_factory=list)
    hidden: list[FieldRef] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.alias:
            raise ValueError("FieldConfiguration requires a non-empty alias")
        if not self.name:
            raise ValueError("FieldConfiguration requires a non-empty name")
        from stint.registry import registry

        registry.register_field_configuration(self)
