# Workflows: `stint.schema.workflows`

Declarations for workflows and workflow schemes. A project binds one with
`Project.__workflow_scheme__`; statuses are tenant-global and referenced by
name; transition `restrict_to` holders become the aggregate
`system:restrict-issue-transition` rule.

See the [Schema guide](../guides/schema.md#workflows-and-workflow-schemes)
for the full walkthrough, including status mappings and the transition
lockout guard.

::: stint.schema.workflows
