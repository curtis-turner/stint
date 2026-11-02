# Schema plane: `stint.schema`

Declarative bases and dataclasses. `IssueType` and `Project` use metaclasses that validate `__alias__` / `__key__` and register each subclass; `Screen`, `ScreenScheme`, `FieldConfiguration`, and the security types are dataclasses that self-register on construction.

See the [schema guide](../guides/schema.md) for rationale and examples.

::: stint.schema
