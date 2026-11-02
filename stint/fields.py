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

_CFT = "com.atlassian.jira.plugin.system.customfieldtypes:"


class _FieldType:
    """Marker base for the Jira custom field type.

    ``jira_type_id`` is the field type; ``searcher_key`` is the JQL searcher
    Jira must attach to make the field searchable. Omitting ``searcherKey``
    on ``POST /field`` creates a field whose search template is None: writes
    and reads work, but any JQL clause on it fails with "Your query couldn't
    be processed" / "field is not searchable". Keys per Atlassian's
    "Essential parameters for custom field creation" table.
    """

    jira_type_id: ClassVar[str] = ""
    searcher_key: ClassVar[str] = ""


class TextField(_FieldType):
    jira_type_id = _CFT + "textfield"
    searcher_key = _CFT + "textsearcher"


class TextAreaField(_FieldType):
    jira_type_id = _CFT + "textarea"
    searcher_key = _CFT + "textsearcher"


class SelectField(_FieldType):
    jira_type_id = _CFT + "select"
    searcher_key = _CFT + "multiselectsearcher"


class MultiSelectField(_FieldType):
    jira_type_id = _CFT + "multiselect"
    searcher_key = _CFT + "multiselectsearcher"


class UserField(_FieldType):
    jira_type_id = _CFT + "userpicker"
    searcher_key = _CFT + "userpickergroupsearcher"


class NumberField(_FieldType):
    jira_type_id = _CFT + "float"
    searcher_key = _CFT + "numberrange"


class DateField(_FieldType):
    jira_type_id = _CFT + "datepicker"
    searcher_key = _CFT + "daterange"


class DateTimeField(_FieldType):
    jira_type_id = _CFT + "datetime"
    searcher_key = _CFT + "datetimerange"


class RadioButtonsField(_FieldType):
    jira_type_id = _CFT + "radiobuttons"
    searcher_key = _CFT + "multiselectsearcher"


class CheckboxesField(_FieldType):
    jira_type_id = _CFT + "multicheckboxes"
    searcher_key = _CFT + "multiselectsearcher"


class LabelsField(_FieldType):
    jira_type_id = _CFT + "labels"
    searcher_key = _CFT + "labelsearcher"


class URLField(_FieldType):
    jira_type_id = _CFT + "url"
    searcher_key = _CFT + "exacttextsearcher"


class VersionField(_FieldType):
    jira_type_id = _CFT + "version"
    searcher_key = _CFT + "versionsearcher"


class MultiVersionField(_FieldType):
    jira_type_id = _CFT + "multiversion"
    searcher_key = _CFT + "versionsearcher"


class GroupField(_FieldType):
    jira_type_id = _CFT + "grouppicker"
    searcher_key = _CFT + "grouppickersearcher"


class MultiGroupField(_FieldType):
    jira_type_id = _CFT + "multigrouppicker"
    searcher_key = _CFT + "multiselectsearcher"


class MultiUserField(_FieldType):
    jira_type_id = _CFT + "multiuserpicker"
    searcher_key = _CFT + "userpickergroupsearcher"


class ReadOnlyField(_FieldType):
    jira_type_id = _CFT + "readonlyfield"
    searcher_key = _CFT + "textsearcher"


# Option-style types: values must come from a fixed, field-owned option list
# (the same list the Jira "add option" API manages). Version/Group/User
# pickers validate against Jira-side objects (versions, groups, users) that
# exist independently of the field, so they don't take `options`.
_SELECT_TYPES = (SelectField, MultiSelectField, RadioButtonsField, CheckboxesField)


@dataclass(frozen=False)
class CustomField:
    """A custom field declaration. Auto-registers with the global registry on creation."""

    alias: str
    name: str
    type: type[_FieldType]
    description: str = ""
    options: list[str] = field(default_factory=list)
    # Declaring module for scoped autogenerate; recorded by the registry at
    # registration time (frame inspection), used to keep a field declared by
    # the --schema module in scope even when no scheme references it yet (M5).
    _declared_module: str = field(default="", repr=False, compare=False)

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
