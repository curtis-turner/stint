---
hide:
  - toc
---

# stint

<p class="custom-subtitle">
Declarative <strong>schema-as-code</strong> and <strong>ORM</strong> for work-management systems. Jira first, dialect protocol for the rest.
</p>

`stint` is two tools in one, in the pattern of SQLAlchemy and Alembic:

1. **Schema-as-code** — declare your Jira Cloud admin objects (custom fields, screens, screen schemes, field configurations, issue types, projects, security schemes) as Python classes in version control, then apply them through **versioned migrations**.
2. **ORM** — use the *same* model classes to insert, read, update, and delete issues, with queries translated to JQL under the hood.

The primary target is **Jira Cloud company-managed** projects. An experimental `jira_cloud_tmp` dialect supports team-managed projects via a terraform-style apply. Jira Data Center is out of scope.

## Install

```bash
uv add stint   # or: pip install stint
```

Requires Python 3.10–3.14.

## Two planes at a glance

=== "Schema as code"

    ```python
    from typing import Annotated, Literal

    from stint import CustomField, SelectField, Screen, \
        ScreenScheme, IssueType, Project

    severity_field = CustomField(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        options=["S1", "S2", "S3", "S4"],
    )

    bug_create_screen = Screen(
        alias="bug_create",
        name="Bug Create Screen",
        fields=["Summary", "Description", "Reporter", severity_field],
    )
    bug_scheme = ScreenScheme(
        alias="bug_screens",
        create=bug_create_screen,
        edit=bug_create_screen,
        view=bug_create_screen,
    )

    class Bug(IssueType):
        __alias__ = "bug"
        __screen_scheme__ = bug_scheme
        severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity_field]

    class Platform(Project):
        __key__ = "PLAT"
        __style__ = "company-managed"
        __issuetypes__ = [Bug]
    ```

    Validate offline, autogenerate a migration, apply it:

    ```console
    $ stint validate --schema schemas/platform.py
    $ stint revision --autogenerate --schema schemas.platform -m "add bug severity"
    $ stint upgrade
    ```

=== "ORM for issues"

    ```python
    from stint import APITokenAuth, StateFile, create_engine, select
    from stint.query import Session

    engine = create_engine(
        "jira_cloud+https://you.atlassian.net",
        auth=APITokenAuth(email="you@example.com", token="..."),
    )
    state = StateFile.load("state/dev.yaml")

    with Session(engine, state) as session:
        s1_bugs = session.scalars(
            select(Bug).where(Bug.c.severity == "S1").limit(10)
        )
        bug = Bug(summary="Crash on save", severity="S1")
        session.add(bug)
        session.commit()   # bug.key is populated
    ```

    Queries compile to JQL (`cf[10042] = "S1"`), resolved through the state
    file's alias → id maps. Updates send only the changed fields.

<div class="grid cards" markdown>

-   :material-shape-plus-outline:{ .lg .middle } __Schema as code__

    ---

    Declare custom fields, screens, schemes, issue types, projects and
    security schemes as Python. Register, validate, and version everything.

    [:octicons-arrow-right-24: Schema guide](guides/schema.md)

-   :material-source-branch:{ .lg .middle } __Versioned migrations__

    ---

    Alembic-style revision graph, autogeneration against a live tenant,
    and an `op` namespace of idempotent operations.

    [:octicons-arrow-right-24: Migrations guide](guides/migrations.md)

-   :material-database-search-outline:{ .lg .middle } __ORM__

    ---

    Sync and async sessions with identity maps, dirty tracking, and
    JQL-backed queries. Partial commits fail loudly.

    [:octicons-arrow-right-24: Querying guide](guides/querying.md)

-   :material-console:{ .lg .middle } __CLI__

    ---

    `validate`, `stamp`, `revision`, `upgrade`, `downgrade`, `current`,
    `history`, `reflect` — and the experimental `apply` for team-managed.

    [:octicons-arrow-right-24: CLI reference](guides/cli.md)

</div>

## Known sharp edges

Jira's admin APIs have real constraints, and stint is honest about them:

- **No transactions.** Jira admin REST has no transactional boundaries; a batch can partially commit. stint raises [`PartialCommitError`](api/query.md) naming each failed operation, and successful work is **not** rolled back.
- **Drift is not auto-reverted.** Changes made outside stint (or another deployment tool) are detected as diffs, not undone.
- **State files are meant to be committed.** They contain alias → id maps and the current revision — no secrets. Credentials come from `STINT_USER` / `STINT_TOKEN` or environment files.

See the [quick start](guides/quick-start.md) for a full end-to-end walkthrough.
