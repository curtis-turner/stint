# pensum

Declarative schema-as-code and ORM for work-management systems. Jira first.

pensum is two tools in one, the same way SQLAlchemy + Alembic is.

1. **Schema as code with versioned migrations.** Declare projects,
   issuetypes, custom fields, screens, and schemes as Pydantic classes.
   Autogenerate a migration from the diff between the declaration and the
   live Jira instance. Commit the migration. Apply it across environments.
2. **ORM.** Insert and query issues through the same model classes.
   `session.add(Bug(...))`, `session.scalars(select(Bug).where(...))`.

Status: alpha (`0.1.0a0`). API may shift before `0.1.0` final.

**Primary target: Jira Cloud**, covering both company-managed and
team-managed projects. Live-instance validation against a real Cloud
tenant is the gating work for `0.1.0` final.

**Jira Data Center is fast-follow.** The DC dialect is in the tree and
exercised by the same test suite, but has not yet been smoke-tested
against a live DC instance. Treat 0.1 DC support as best-effort until
that validation lands. Plenty of teams will sit on DC for years, and the
architecture accommodates them. The validation gap is the only thing
holding DC back from parity.

## Install

```bash
pip install pensum
```

Python 3.10 or newer. The only required runtime deps are `pydantic`,
`httpx`, and `pyyaml`.

## Why

Jira admin lives in a web UI. That makes it hard to review changes, hard
to mirror across environments, and impossible to roll back cleanly.
pensum treats Jira as a database where the schema happens to be
unusually heavy. The schema is Python you commit. Migrations are Python
you commit. Two environments get the same shape by running the same
migration chain.

The data plane is a separate concern. Once the schema is under
management, the same model classes are the ORM. `Bug.severity == "S1"`
compiles to `cf[10042] = "S1"` (or whatever the per-env id is, resolved
through the state file).

## A small example

Define the schema as Pydantic classes:

```python
# schemas/platform.py
from typing import Annotated, Literal

from pensum import (
    CustomField, IssueType, Project, Screen, ScreenScheme,
    FieldConfiguration, SelectField, TextField,
)

severity_field = CustomField(
    alias="bug_severity",
    name="Severity",
    type=SelectField,
    options=["S1", "S2", "S3", "S4"],
)

bug_create = Screen(alias="bug_create", name="Bug Create",
                   fields=["Summary", "Reporter", severity_field])
bug_edit = Screen(alias="bug_edit", name="Bug Edit",
                  fields=["Summary", "Reporter", severity_field])
bug_view = Screen(alias="bug_view", name="Bug View",
                  fields=["Summary", "Reporter", severity_field])

bug_screens = ScreenScheme(
    alias="bug_screens", name="Bug Screen Scheme",
    create=bug_create, edit=bug_edit, view=bug_view,
)
bug_fields = FieldConfiguration(
    alias="bug_fields", name="Bug Field Config", required=["Summary"],
)

class Bug(IssueType):
    __alias__ = "bug"
    __screen_scheme__ = bug_screens
    __field_configuration__ = bug_fields

    key: str | None = None
    summary: str
    reporter: str
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]

class Platform(Project):
    __key__ = "PLAT"
    __lead__ = "cturner"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
```

Validate it without any network call:

```bash
pensum validate --schema schemas/platform.py
```

Generate a migration from the diff between your schema and the live env:

```bash
export PENSUM_TOKEN=...        # PAT for Jira DC
pensum revision --autogenerate \
    --schema schemas.platform \
    --state state/dev.yaml \
    --migrations-dir migrations/ \
    --url jira_dc+https://jira.example.com \
    --auth pat \
    --env dev \
    -m "add bug severity"
```

Review the emitted Python file. It looks roughly like:

```python
# migrations/2026_05_25_1430_add_bug_severity.py
from pensum import op
from pensum.fields import SelectField

revision = "abc123def456"
down_revision = None

async def upgrade():
    await op.create_custom_field(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        options=["S1", "S2", "S3", "S4"],
    )

async def downgrade():
    op.unsupported("deleting bug_severity destroys severity data on existing issues")
```

Apply it:

```bash
pensum upgrade --env dev
```

Then query and write issues through the same classes:

```python
from pensum import Session, StateFile, PATAuth, create_engine, select
from schemas.platform import Bug

state = StateFile.load("state/dev.yaml")
engine = create_engine("jira_dc+https://jira.example.com", auth=PATAuth(token))

with Session(engine, state) as session:
    # READ
    s1s = session.scalars(select(Bug).where(Bug.c.severity == "S1"))

    # WRITE
    bug = Bug(summary="boom", reporter="alice", severity="S2")
    session.add(bug)
    session.commit()
    print(bug.key)              # populated from the response

    # UPDATE
    existing = session.get(Bug, "PLAT-1234")
    existing.severity = "S1"
    session.commit()            # only the changed field is sent
```

The async version is `AsyncSession(engine, state)` with the same surface
plus `await` on the I/O methods.

## What ships in 0.1

- **Jira Cloud (primary)**: company-managed and team-managed projects.
  Live-tenant smoke is the gating work for `0.1.0` final.
- **Jira Data Center (`9.12` LTS and newer, fast-follow)**: same code
  paths via the shared dialect base; awaits live-instance smoke testing.
- Schema plane: custom fields, screens, screen schemes, issue type
  screen schemes, field configurations, field configuration schemes,
  issue types, projects.
- Migrations: autogenerate, multi-head merge, upgrade, downgrade,
  brownfield stamp, idempotent ops, advisory lock, `429`/`503` retry.
- Data plane: reads and writes, identity map, dirty tracking, ADF
  wrapping for Cloud descriptions.
- Cloud team-managed projects: project create with style tracking, CMP-only
  ops fail loud with a Jira UI deep link. Per-issuetype inline screen field
  lists deferred until Atlassian exposes them through REST.

## What does not ship in 0.1

- Workflows and workflow schemes.
- Permission and notification schemes.
- ADF parsing on reads (writes wrap plain text correctly).
- Backends other than Jira. The dialect protocol is the extension point.

## CLI

```
pensum reflect    Reflect a Jira instance into a snapshot and print it.
pensum revision   Create a migration (empty, --merge, or --autogenerate).
pensum stamp      Brownfield: match aliases against existing Jira, populate state.
pensum upgrade    Apply pending migrations to head (or --to <rev>).
pensum downgrade  Roll back to a specific revision.
pensum current    Show the env's current revision.
pensum history    List migrations in revision order.
pensum validate   Run schema-level checks on a Python schema module.
```

## Caveats

- **No transactions across or within migrations.** Jira admin REST does
  not provide them. A failed migration leaves Jira in a partial state.
  Re-running picks up where it left off because op functions check the
  alias-to-id map first.
- **Partial commits in the data plane raise `PartialCommitError`.** Some
  inserts can succeed and later ones fail; the exception carries the
  per-instance breakdown so callers can decide what to do.
- **Drift is not auto-reverted.** UI edits land in the live instance and
  stay there until you reflect, diff, and absorb them into the schema.

## License

MIT. See [LICENSE](LICENSE).
