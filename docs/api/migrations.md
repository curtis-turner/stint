# Migrations: `stint.migrations`

Alembic-style migration machinery: `Migration` units loaded into a `RevisionGraph`, a function-based `op` namespace of async, idempotent operations, and the upgrade/downgrade runners. `get_context()` returns the active `MigrationContext` inside a migration.

See the [migrations guide](../guides/migrations.md) for the workflow.

::: stint.migrations
