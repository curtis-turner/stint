# Migrations

stint's migration story mirrors Alembic: a chainable set of Python migration units, an autogeneration path that diffs your schema against a live tenant, and an operation namespace (`op`) that applies changes idempotently.

## The revision graph

A migration file defines plain variables and functions:

```python
revision = "20261001_120000"      # unique id
down_revision = "20260915_090000" # parent in the DAG (None = base)

async def upgrade():
    await op.create_custom_field(...)

async def downgrade():
    ...
```

`load_migrations(directory)` builds a `RevisionGraph` that

- enforces a single-parent chain,
- supports merge revisions (two `down_revision`s),
- exposes `heads()`, `bases()`, `chain_from()`, `all_ordered()`.

Autogeneration requires a **clean head** — only one head and no uncommitted changes unless you pass `--force` (or `--merge` to create a merge revision deliberately).

## Generating and applying

```console
# diff schema vs live tenant, write a migration + update state
$ stint revision --autogenerate --schema schemas.platform \
    --state state/dev.yaml --migrations-dir migrations/ --env dev \
    -m "add bug severity"

# run pending migrations
$ stint upgrade --state state/dev.yaml --env dev

# inspect
$ stint current
$ stint history

# roll back one step, or to a revision / all the way back
$ stint downgrade --state state/dev.yaml --env dev
$ stint downgrade --revision base
```

`upgrade --to <revision>` stops at a given revision instead of running everything.

## The `op` namespace

Inside `upgrade()` / `downgrade()` the `op` module carries the whole operation vocabulary. Every op resolves objects through the **state file's alias → id maps**, which is what makes ops idempotent and safe to re-run. Highlights:

| Area | Operations |
|---|---|
| Custom fields | `create_custom_field`, `update_custom_field`, `add_custom_field_option`, `remove_custom_field_option`, `delete_custom_field` |
| Screens | `create_screen`, `update_screen`, `delete_screen`, `add_screen_tab`, `add_screen_tab_field` |
| Screen schemes | `create_screen_scheme`, `update_screen_scheme`, `delete_screen_scheme` |
| Issue-type schemes | `create_issuetype_screen_scheme`, `update_issuetype_screen_scheme`, `delete_issuetype_screen_scheme` |
| Field configurations | `update_field_configuration`, `delete_field_configuration`, `create_field_configuration_scheme`, `update_field_configuration_scheme`, `delete_field_configuration_scheme` |
| Issue types / projects | `create_issuetype`, `update_issuetype`, `delete_issuetype`, `create_project`, `update_project`, `delete_project`, `create_issuetype_scheme`, `update_issuetype_scheme`, `delete_issuetype_scheme` |
| Security | `create_issue_security_scheme`, `add_issue_security_level`, `add_issue_security_level_member`, `clear_default_issue_security_level`, `set_project_issue_security_scheme`, `delete_issue_security_scheme` |

All ops are `async`. Inside a migration you can grab the live context:

```python
from stint import get_context

async def upgrade():
    ctx = get_context()   # MigrationContext: engine + state
    ...
    await ctx.persist()   # checkpoint the state file to disk mid-migration
```

### Refusing to guess: `unsupported()`

When the surrounding migration can't run safely (e.g. a destructive downgrade stint can't prove correct), call:

```python
async def downgrade():
    op.unsupported("cannot safely remove a select option without re-reading options")
```

This raises `UnsupportedDowngradeError` rather than performing a wrong guess.

## The state file

`state/dev.yaml` (path is your choice) records, per environment:

- alias → Jira id maps for every declared object,
- the current migration revision,
- reflected snapshot data (`Snapshot`, `ServerInfoSnapshot`, ...).

Environment name (`--env`) plus `--url` / `--auth` flags, or `~/.stint/envs/dev.yaml`, supply the tenant connection. A process-level advisory lock guards concurrent applies on one machine; for CI, run `stint upgrade` from your pipeline so state is written in exactly one place. State files are safe to commit — secrets live only in `STINT_USER` / `STINT_TOKEN`.

## Brownfield: populating state from an existing instance

Adopting an existing Jira workspace? `stint stamp` populates the state by matching your schema declarations against objects that already exist in the tenant — **read-only, no writes** — so the first migration doesn't try to create what's already there:

```console
$ stint stamp --schema schemas.platform --state state/dev.yaml --env dev
```

## Honest limitations

- Jira admin REST has **no transactions**; a partially-failing `upgrade` leaves earlier ops applied. Re-running converges (ops are idempotent), but read the error output first.
- Drift — objects changed outside stint (or by other tools) — shows up as diffs at autogeneration time; stint never auto-reverts it.
- The company-managed path is the primary migration surface. Team-managed projects use the experimental [`stint apply` flow](https://github.com/curtis-turner/stint/tree/main/examples/team_managed) instead (terraform-style plan + confirm).
