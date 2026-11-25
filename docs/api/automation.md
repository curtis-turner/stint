# Automation rules: `stint.schema.automation`

Declarations for Jira Automation rules and rule schemes. A project binds one
scheme with `Project.__automation_rule_scheme__`; every rule in it is scoped
to the bound projects (one multi-project scope, never copies). Rules are
reflected, diffed, applied, and recorded in state by uuid.

Two declarations:

- `AutomationRule` — a rule whose payload comes from a JSON file (the
  Automation UI's `GET rule/{uuid}` export), with tenant ids replaced by
  `{{stint.*}}` references that stint substitutes at apply time.
- `ControlFieldGuard` — the detect-and-revert rule generated from the
  schema's control fields and allowed writers. The enforcement layer the
  restricted-Edit-screen pattern cannot provide (the screen lock is
  cosmetic for API clients since the override-screen-security
  deprecation, JRACLOUD-98054).

See the [Schema guide](../guides/schema.md#automation-rules-and-the-control-field-guard)
for the full walkthrough and the
[Locking guide](../guides/locking.md) for the threat model.

::: stint.schema.automation
