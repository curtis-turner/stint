# Schema as code

Everything in the **schema plane** is a Python declaration in your repo. stint imports it, registers every object under its alias, validates cross-references, and turns it into migration operations. There are no decorators: declarativeness comes from base classes with metaclasses (`IssueType`, `Project`), and dataclasses that self-register on construction (`CustomField`, `Screen`, `ScreenScheme`, `FieldConfiguration`, security-scheme types).

## Custom fields

`CustomField` binds a stable Python-side **alias** to a Jira custom field:

```python
from stint import CustomField, MultiSelectField, SelectField, TextField, UserField

severity_field = CustomField(
    alias="bug_severity",
    name="Severity",
    type=SelectField,  # a field-type class, not an instance
    description="Bug severity classification",
    options=["S1", "S2", "S3", "S4"],
)

tags_field = CustomField(alias="release_tags", name="Tags", type=MultiSelectField)
```

- `alias` is the stable name used in imports, state files, and migrations — Jira's numeric field ids change per domain.
- `type` maps to a Jira customfield type **plus** its JQL searcher key; without the right searcher, JQL clauses over the field fail.
- `options` may only be given to (and are required by) elective types: `SelectField`, `MultiSelectField`, `RadioButtonsField`, `CheckboxesField`.

### Field types

`stint.fields` provides 18 marker classes, each carrying its Jira type id and searcher key:

| Category | Types |
|---|---|
| Text | `TextField`, `TextAreaField`, `ReadOnlyField` |
| Selection (need `options`) | `SelectField`, `MultiSelectField`, `RadioButtonsField`, `CheckboxesField` |
| Numbers / dates | `NumberField`, `DateField`, `DateTimeField` |
| People | `UserField`, `MultiUserField`, `GroupField`, `MultiGroupField` |
| Links / versions | `URLField`, `VersionField`, `MultiVersionField` |
| Freeform | `LabelsField` |

## Screens, screen schemes, field configuration

These are company-managed concepts. `Screen` is an ordered list of fields — strings are Jira system field names, `CustomField` instances are your declared fields:

```python
from stint import FieldConfiguration, Screen, ScreenScheme

bug_create_screen = Screen(
    alias="bug_create",
    name="Bug Create Screen",
    fields=["Summary", "Description", "Reporter", severity_field],
)

bug_screen_scheme = ScreenScheme(
    alias="bug_screens",
    name="Bug Screen Scheme",
    create=bug_create_screen,  # CMP: separate screens per operation
    edit=bug_edit_screen,
    view=bug_view_screen,
)

bug_field_config = FieldConfiguration(
    alias="bug_fields",
    name="Bug Field Configuration",
    required=["Summary", "Reporter", severity_field],
    hidden=[],  # optional
)
```

## Issue types

`IssueType` subclasses play a **dual role**: Jira-side schema metadata (`__alias__`, `__screen_scheme__`, `__field_configuration__`, ...) and a Pydantic model for the [data plane](querying.md).

```python
from typing import Annotated, Literal
from stint import IssueType


class Bug(IssueType):
    __alias__ = "bug"  # required, stable identity
    __title__ = "Bug"  # defaults to the class name
    __description__ = "Defects in production"
    __screen_scheme__ = bug_screen_scheme
    __field_configuration__ = bug_field_config

    summary: str  # system field
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]  # custom field
    root_cause: Annotated[str | None, root_cause_field] = None  # optional custom field
```

Rules the metaclass enforces at definition time:

- `__alias__` is required and must be unique (re-declaring from the *same module* replaces — that keeps imports from clobbering; a clash from a different module raises `RegistryError`).
- Custom-field attributes **must** use `Annotated[T, <CustomField instance>]` so the schema plane can find them.
- Constraining literals (e.g. `Literal["S1", ...]`) ride along: constructed instances are Pydantic-validated against them.

## Projects

```python
from stint import Project


class Platform(Project):
    __key__ = "PLAT"  # required
    __title__ = "Platform"
    __lead__ = "cturner@example.com"
    __style__ = "company-managed"  # or the experimental "team-managed"
    __issuetypes__ = [Bug]
```

!!! note
    `__lead__` requires the token owner to hold the "Browse users and groups" permission. Built-in issue types (`Bug`, `Task`, ...) that you reference are **adopted**, not recreated.

## Security schemes

Issue-security schemes are declared as dataclasses; emails in members are resolved to account ids at apply time:

```python
from stint import SecurityLevel, SecurityLevelMember, SecurityScheme

internal_level = SecurityLevel(
    name="Internal Only",
    members=[SecurityLevelMember(type="group", name="platform-team")],
)
public_level = SecurityLevel(
    name="Public",
    members=[SecurityLevelMember(type="user", name="cturner@example.com")],
)

bug_security = SecurityScheme(
    alias="bug_security",
    name="Bug Security",
    levels=[internal_level, public_level],
    default_level=internal_level,
)
```

Wire it to a project through `op.set_project_issue_security_scheme` in a migration.

## Validation and the registry

- [`stint.validate()`](../api/validate.md) collects human-readable problems (project → issue type, screen → custom field, scheme → screen, ...) without raising.
- [`stint.validate_or_raise()`](../api/validate.md) raises `ConfigurationError` on the first batch of problems.
- The [`registry`](../api/registry.md) singleton tracks every declared object by alias; the CLI `stint validate` command runs checks against a schema module with **no network calls**.

Everything here lands in the [state file](migrations.md#the-state-file) when migrations apply — the alias → id map that powers migrations and JQL later.
