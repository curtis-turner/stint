# Competitive landscape

Research date: 2026-06-25. Captures what exists for managing work-management
config as code, and where stint has room.

## Jira

A Jira Terraform provider exists but does not threaten stint.

[fourplusone/terraform-provider-jira](https://github.com/fourplusone/terraform-provider-jira)

- Latest release v0.1.20, January 2023. Over three years untouched.
- Targets JIRA 7.x (Server/DC). Cloud compatibility listed as "untested".
- Manages issues, issue types, projects, fields, users, groups, filters.
- Does NOT manage screens, screen schemes, field configurations, or issue
  type screen schemes. That config plane is exactly what stint owns.
- Procedural HCL. No migration history. No ORM data plane.

No SQLAlchemy/Alembic-style migrations-plus-ORM tool for Jira exists. The
combination stint targets (versioned migrations and a typed ORM over the
screen/scheme/field-config plane, on Cloud) is unoccupied.

## Linear

Linear already has an actively maintained config-as-code option, so stint
would not be first here.

[terraform-community-providers/linear](https://github.com/terraform-community-providers/linear)

- v0.3.7, released April 2026. 22 releases. Actively maintained.
- Covers core Linear config: teams, labels, workflow states, projects,
  memberships.
- Small adoption (~5 stars), but current and working.

Linear users who want config-as-code already have a declarative Terraform
path. The Linear pitch cannot lean on an empty gap.

## Where stint differs from any Terraform provider

Two axes are the real moat, not being first.

1. Migration history vs state reconciliation. Terraform owns desired state
   and auto-reconciles, silently correcting drift on apply. stint is
   migration-based like Alembic: an ordered, auditable chain of changes,
   and it deliberately does not auto-revert drift.
2. ORM data plane. No Terraform provider gives a typed query and write API
   for the issues themselves. `session.scalars(select(Bug)...)` has no
   equivalent in either provider. Hardest thing for a Terraform-shaped
   competitor to copy.

## Implications

- Jira pitch is clean. Closest competitor is stale, DC-era, and skips the
  core plane.
- Linear pitch needs to frame the difference (migration history plus ORM
  over Terraform state) and treat appetite as an open question, not a gap.

## Sources

- https://github.com/fourplusone/terraform-provider-jira
- https://github.com/terraform-community-providers/linear
- https://registry.terraform.io/providers/terraform-community-providers/linear/latest/docs
- https://linear.app/developers/graphql
- https://github.com/sqlalchemy/alembic
