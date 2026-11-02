# Quick start

A complete walk through the stint workflow, adapted from the
[company-managed example](https://github.com/curtis-turner/stint/tree/main/examples/company_managed):
declare a schema, validate it offline, autogenerate a migration, apply it to a live tenant, and read/write issues through the ORM.

Prerequisites:

- Python 3.10+
- A [Jira Cloud API token](https://support.atlassian.com/atlassian-account/kb/using-personal-access-tokens/), with **site-admin** permission on the tenant
- `stint` installed (`uv add stint`)

## 1. Declare your schema

Create `schemas/platform.py`:

```python
from datetime import datetime
from typing import Annotated, Literal

from stint import (
    CustomField,
    FieldConfiguration,
    IssueType,
    Project,
    Screen,
    ScreenScheme,
    SelectField,
    TextField,
)

# Custom fields. Select-style types require `options`.
severity_field = CustomField(
    alias="bug_severity",
    name="Severity",
    type=SelectField,
    description="Bug severity classification",
    options=["S1", "S2", "S3", "S4"],
)
root_cause_field = CustomField(alias="bug_root_cause", name="Root Cause", type=TextField)

# Screens: strings are system field names, CustomField instances
# are your declared fields.
bug_create_screen = Screen(
    alias="bug_create",
    name="Bug Create Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field],
)
bug_edit_screen = Screen(
    alias="bug_edit",
    name="Bug Edit Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field, root_cause_field],
)
bug_view_screen = Screen(
    alias="bug_view",
    name="Bug View Screen",
    fields=["Summary", "Description", "Reporter", "Assignee", severity_field, root_cause_field, "Created", "Updated"],
)

bug_screen_scheme = ScreenScheme(
    alias="bug_screens",
    name="Bug Screen Scheme",
    create=bug_create_screen,
    edit=bug_edit_screen,
    view=bug_view_screen,
)

bug_field_config = FieldConfiguration(
    alias="bug_fields",
    name="Bug Field Configuration",
    required=["Summary", "Reporter", severity_field],
)


# Issue type: dual role. Python-side it is a Pydantic model you'll
# construct and query; Jira-side it carries the screen scheme and
# custom-field wiring.
class Bug(IssueType):
    __alias__ = "bug"
    __description__ = "Defects in production"
    __screen_scheme__ = bug_screen_scheme
    __field_configuration__ = bug_field_config

    # System fields are bare annotations; custom fields use
    # Annotated[T, <CustomField instance>].
    summary: str
    description: str | None = None
    reporter: str
    assignee: str | None = None
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]
    root_cause: Annotated[str | None, root_cause_field] = None
    created: datetime | None = None
    updated: datetime | None = None


class Platform(Project):
    __key__ = "PLAT"
    __lead__ = "cturner@example.com"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
```

!!! tip "Naming convention"
    Give the module-level `CustomField` a `_field` / `_cf` suffix so it
    can't collide with the class-attribute name used inside `Annotated[]`.
    Without it, a class attribute with a default value can shadow the
    declaration while type hints resolve.

## 2. Validate offline (no network)

```console
$ stint validate --schema schemas/platform.py
```

This runs reference checks — project → issue type, screen → custom field,
scheme → screen — and prints every problem it finds. Empty output means the
schema is consistent.

## 3. Autogenerate a migration

Set credentials and diff the declared schema against the live tenant:

```console
$ export STINT_USER='you@example.com' STINT_TOKEN='...'
$ stint revision --autogenerate \
    --schema schemas.platform \
    --state state/dev.yaml \
    --migrations-dir migrations/ \
    --url jira_cloud+https://you.atlassian.net \
    --auth api-token \
    --env dev \
    -m "create PLAT project with bug severity"
```

The emitted file looks like:

```python
revision = "20241001_120000"
down_revision = None


async def upgrade():
    await op.create_custom_field(...)
    ...


async def downgrade(): ...
```

!!! note
    `--env` resolves shared connection settings (URL, auth style, token/user
    env-var names) from `~/.stint/envs/dev.yaml`, so each developer and CI
    point at their own tenant with one word. After autogeneration, commit
    both `migrations/` and the freshly written `state/dev.yaml`.

## 4. Apply to the environment

```console
$ stint upgrade --state state/dev.yaml --env dev
```

`upgrade` executes pending migrations in revision order. Operations are
idempotent: they resolve objects through the state file's alias → id maps,
so re-runs converge. State files contain **no secrets** — commit them.

## 5. Query and write issues

Once the migration has stood up `PLAT`, the data plane works through the
same `Bug` class:

```python
import asyncio
from stint import APITokenAuth, StateFile, create_engine
from stint.query import AsyncSession, or_, select


async def main():
    engine = create_engine(
        "jira_cloud+https://you.atlassian.net",
        auth=APITokenAuth(
            email="you@example.com",
            token=os.environ["STINT_TOKEN"],
        ),
    )
    state = StateFile.load("state/dev.yaml")

    async with AsyncSession(engine, state) as session:
        # Insert
        bug = schema.Bug(summary="Crash on save", severity="S1")
        session.add(bug)  # project inferred from the schema
        results = await session.commit()
        print(bug.key)  # e.g. PLAT-123, populated on commit

        # Read one by key (hydrates custom fields via the state file)
        first = await session.get(schema.Bug, bug.key, fresh=True)

        # Filtered query — compiles to JQL under the hood
        s1 = await session.scalars(
            select(schema.Bug).where(schema.Bug.c.severity == "S1").order_by(schema.Bug.c.created, "DESC").limit(10)
        )

        # or-combined predicates
        urgent = await session.scalars(
            select(schema.Bug).where(or_(schema.Bug.c.severity == "S2", schema.Bug.c.severity == "S1"))
        )

        # Update: PUT sends only changed fields
        bug.severity = "S2"
        await session.commit()

        # Delete
        session.delete(bug)
        await session.commit()

    await engine.close()


asyncio.run(main())
```

A sync mirror exists: `from stint.query import Session` — same surface minus `await`.

## Where to go next

- [Schema as code](schema.md) — every declaration object and the field-type catalogue
- [Migrations](migrations.md) — the revision graph, hand-editing migrations, brownfield `stint stamp`
- [Querying and writing](querying.md) — identity map, dirty tracking, `PartialCommitError`
- The experimental [team-managed walkthrough](https://github.com/curtis-turner/stint/tree/main/examples/team_managed) uses `stint apply` instead of migrations.
