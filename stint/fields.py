"""Custom field declarations.

Standalone Pydantic-validated objects that describe a Jira custom field at the
schema level. The Pydantic-typed attribute on the IssueType class controls
data-plane validation. CustomField controls schema-plane planning and applying.

The two are linked at use time, not at declaration time: a Screen references
the CustomField, the IssueType declares a Pydantic attribute with the matching
name, and the dialect's reflection/state-file lookup ties them together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


class _FieldType:
    """Marker base for the Jira custom field type."""

    jira_type_id: ClassVar[str] = ""


class TextField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:textfield"


class TextAreaField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:textarea"


class SelectField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:select"


class MultiSelectField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:multiselect"


class UserField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:userpicker"


class NumberField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:float"


class DateField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:datepicker"


class DateTimeField(_FieldType):
    jira_type_id = "com.atlassian.jira.plugin.system.customfieldtypes:datetime"


_SELECT_TYPES = (SelectField, MultiSelectField)


@dataclass(frozen=False)
class CustomField:
    """A custom field declaration. Auto-registers with the global registry on creation."""

    alias: str
    name: str
    type: type[_FieldType]
    description: str = ""
    options: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.alias:
            raise ValueError("CustomField requires a non-empty alias")
        if not self.name:
            raise ValueError("CustomField requires a non-empty name")
        if self.options and self.type not in _SELECT_TYPES:
            raise ValueError(
                f"CustomField {self.alias!r}: options only valid for select-style types, got {self.type.__name__}"
            )
        if self.type in _SELECT_TYPES and not self.options:
            raise ValueError(f"CustomField {self.alias!r}: select-style fields require options")
        # Lazy import to avoid cycles at module-load time.
        from stint.registry import registry

        registry.register_custom_field(self)
