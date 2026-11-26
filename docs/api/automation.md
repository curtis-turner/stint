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

Both project styles are supported. The rules are tenant objects scoped by
project id, so a team-managed project binds the same scheme and reconciles
through the team-managed apply path (`stint apply --dialect jira_cloud_tmp`)
with the same cores. Team-managed guards declare `fields=` explicitly (no
screens to derive control fields from) and allow-list users/groups only
(team-managed projects have no classic project roles).

See the [Schema guide](../guides/schema.md#automation-rules-and-the-control-field-guard)
for the full walkthrough and the
[Locking guide](../guides/locking.md) for the threat model.

::: stint.schema.automation
