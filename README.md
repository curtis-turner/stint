# stint

![stint: Jira admin as code, not clickops](https://raw.githubusercontent.com/curtis-turner/stint/main/assets/readme-hero.png)

Declarative schema-as-code and ORM for work-management systems. Jira first.

stint is two tools in one, the same way SQLAlchemy + Alembic is.

1. **Schema as code with versioned migrations.** Declare projects,
   issuetypes, custom fields, screens, and schemes as Pydantic classes.
   Autogenerate a migration from the diff between the declaration and the
   live Jira instance. Commit the migration. Apply it across environments.
2. **ORM.** Insert and query issues through the same model classes.
   `session.add(Bug(...))`, `session.scalars(select(Bug).where(...))`.

## The problem, in one screen

Changing a Jira project today means clicking through the admin web UI. No
diff. No review. No record of who changed what or why. Staging and
production drift apart and nobody can reconstruct how.

With stint the same change is a commit your teammate can review:

```python
# migrations/2026_06_25_1500_add_bug_severity.py
async def upgrade():
    await op.create_custom_field(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        options=["S1", "S2", "S3", "S4"],
    )
```

CI applies that file to staging, then production, from the same source. A
bad change rolls back to a known revision. The schema lives in your repo
next to the code that depends on it.

Status: alpha (`0.1.0a0`). API may shift before `0.1.0` final.

**Primary target: Jira Cloud**, covering both company-managed and
team-managed projects. Live-instance validation against a real Cloud
tenant is the gating work for `0.1.0` final.

**Jira Data Center is out of scope, not on the roadmap.** An audit against
Atlassian's official OpenAPI specs found that ~17 of the admin endpoints
stint drives exist only on Cloud; DC keeps those objects web-admin-only and
never added REST. A DC dialect would fail on a large part of the op surface,
so stint does not target it. Future growth goes to other work-management
backends, like Linear, through the dialect protocol.

## Install

```bash
pip install stint
```

Python 3.14 or newer. stint relies on [PEP 649](https://peps.python.org/pep-0649/)
deferred annotation evaluation, the default from 3.14, so the schema metaclass
can inspect `Annotated` field metadata (and catch mistakes like a CustomField
that shadows its attribute name) without eagerly resolving every type. Runtime
deps are `pydantic` (with the `email` extra), `pydantic-settings`, `httpx`,
`pyyaml`, and `cyclopts` for the CLI. `cyclopts` pulls in `rich` for terminal
output.

## Why

Jira admin lives in a web UI. That makes it hard to review changes, hard
to mirror across environments, and impossible to roll back cleanly.
stint treats Jira as a database where the schema happens to be
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

from stint import (
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

    summary: str
    reporter: str
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]

class Platform(Project):
    __key__ = "PLAT"
    __lead__ = "cturner@example.com"
    __style__ = "company-managed"
    __issuetypes__ = [Bug]
```

Validate it without any network call:

```bash
stint validate --schema schemas/platform.py
```

Generate a migration from the diff between your schema and the live env:

```bash
export STINT_USER=you@example.com   # Jira Cloud account email
export STINT_TOKEN=...               # Jira Cloud API token
stint revision --autogenerate \
    --schema schemas.platform \
    --state state/dev.yaml \
    --migrations-dir migrations/ \
    --url jira_cloud+https://you.atlassian.net \
    --auth api-token \
    --env dev \
    -m "add bug severity"
```

Review the emitted Python file. It looks roughly like:

```python
# migrations/2026_05_25_1430_add_bug_severity.py
from stint import op
from stint.fields import SelectField

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
stint upgrade --env dev
```

Then query and write issues through the same classes:

```python
from stint import Session, StateFile, APITokenAuth, create_engine, select
from schemas.platform import Bug

state = StateFile.load("state/dev.yaml")
engine = create_engine(
    "jira_cloud+https://you.atlassian.net",
    auth=APITokenAuth(email="you@example.com", token=token),
)

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

- **Jira Cloud (only target in 0.1)**: company-managed and team-managed
  projects. Live-tenant smoke is the gating work for `0.1.0` final.
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
stint reflect    Reflect a Jira instance into a snapshot and print it.
stint revision   Create a migration (empty, --merge, or --autogenerate).
stint stamp      Brownfield: match aliases against existing Jira, populate state.
stint upgrade    Apply pending migrations to head (or --to <rev>).
stint downgrade  Roll back to a specific revision.
stint current    Show the env's current revision.
stint history    List migrations in revision order.
stint validate   Run schema-level checks on a Python schema module.
```

## State files

Each env has a state file (`state/<env>.yaml`) mapping schema aliases to Jira
object IDs, plus the current migration revision. **Commit it.** It is the
shared source of truth for a tenant, and it is not sensitive: it holds object
IDs, the tenant URL, and a revision pointer — no credentials, tokens, issue
data, or PII. Secrets stay in environment variables (`STINT_TOKEN` /
`STINT_USER`); they never touch the state file. This is unlike Terraform state,
which can embed secrets and warrants an encrypted backend.

- **Rebuildable.** Lose or corrupt a state file and `stint stamp` reflects the
  tenant and reconstructs the ID mapping by name/key. Only the revision pointer
  is not auto-derived; set it with `stamp --revision <rev>`.
- **The lock is local.** `stint upgrade` takes an advisory `<state>.lock` on
  the local filesystem. It serializes applies on one machine, not across
  teammates or CI on different machines.
- **Coordinate writers.** Two people running `upgrade` against the same tenant
  at once can double-apply ops and diverge the state file. The simplest safe
  pattern is to **apply from CI on `main`**, so applies serialize and the
  committed state stays canonical. Cross-machine locking is tracked in #12.

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
- **Project lead resolution needs user-search access.** `__lead__` takes an
  email, which stint resolves to a backend user id (DC username, Cloud
  accountId) via the user-search API at apply time. That call requires the
  "Browse users and groups" global permission. Without it the create/update
  fails with guidance; set `__lead__` to an already-resolved username or
  accountId to skip resolution.
- **Built-in issue types are adopted, not recreated.** Every Jira tenant ships
  Bug, Task, Story, Epic, and Subtask. A schema declaring one of those names
  (the quickstart's `Bug` does) adopts the existing type into state on
  `upgrade` rather than failing on a duplicate-name create. Names are matched
  exactly, so `Bug` and `bug` are distinct.
- **Autogenerate needs a clean migration head.** `revision --autogenerate`
  diffs against the state file, which only advances on `upgrade`/`stamp`. With
  unapplied migrations pending it refuses rather than restating them; apply
  first, or pass `--force`.

## License

MIT. See [LICENSE](LICENSE).
