# Your Jira config is production infrastructure. You manage it like a whiteboard.

A teammate pings you on a Friday afternoon.

"Hey, why does the Bug type have a Severity field in staging but not in prod?"

You don't know. Nobody knows. Someone added it months ago by clicking through the admin UI. There's no commit. No ticket. No record of who did it or why. The two environments drifted, and now a release behaves differently depending on where it runs.

You fix it the only way Jira lets you: open the admin UI in two tabs and click until the screens match. By eye. On a Friday.

This is normal. That's the problem.

## Jira admin is a database with no schema tooling

Think about what a Jira project actually is.

- Issue types
- Custom fields
- Screens and screen schemes
- Field configurations
- Workflows

That's a schema. Your team's tickets are rows. Automations, dashboards, and integrations all read against that shape. Break the shape and you break the things downstream.

Now think about how you change that schema. You log into a web UI and click. There is no diff. No review. No version. No rollback. No second pair of eyes before the change lands on the instance your whole company depends on.

We stopped managing application databases this way twenty years ago. Migrations gave us reviewable, repeatable, reversible schema change. Jira config never got that treatment, even though the blast radius is just as large.

## What clickops actually costs you

The web UI feels fine for one change. The cost shows up later.

No review. A field rename or a screen edit ships the instant you click save. No pull request. No approval. The first time anyone notices is when a board stops loading.

No history. Six months from now, "why is this field required?" has no answer. The UI shows the current state and nothing else. There is no log of intent.

Drift. Staging and production were set up by hand at different times by different people. They were never identical and they never will be, until something breaks and you spend an afternoon reconciling them tab by tab.

No clean rollback. A bad migration in a real database rolls back to a known revision. A bad Jira change rolls back to "whatever you can remember clicking, in reverse, hopefully."

These aren't edge cases. They're the default experience of administering Jira at any real scale.

## The fix is boring, and that's the point

We already solved this problem for databases. Treat the schema as code. Generate migrations from the diff. Commit them. Apply the same chain everywhere.

That's the whole idea behind stint.

Declare your Jira schema as Python classes:

```python
from typing import Annotated, Literal
from stint import CustomField, IssueType, Project, SelectField

severity = CustomField(
    alias="bug_severity",
    name="Severity",
    type=SelectField,
    options=["S1", "S2", "S3", "S4"],
)

class Bug(IssueType):
    __alias__ = "bug"
    summary: str
    reporter: str
    severity: Annotated[Literal["S1", "S2", "S3", "S4"], severity]

class Platform(Project):
    __key__ = "PLAT"
    __issuetypes__ = [Bug]
```

Generate a migration from the difference between that declaration and your live instance:

```bash
stint revision --autogenerate -m "add bug severity"
```

stint writes a plain Python file you can read and review:

```python
async def upgrade():
    await op.create_custom_field(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        options=["S1", "S2", "S3", "S4"],
    )
```

Commit it. CI applies it to staging, then production, from the same source. The Friday question answers itself: the Severity field exists because of a reviewed commit, and you can name the author, the date, and the reason.

The same model classes double as a typed query layer for reads and writes:

```python
s1s = session.scalars(select(Bug).where(Bug.c.severity == "S1"))
session.add(Bug(summary="boom", reporter="alice", severity="S2"))
session.commit()
```

It compiles to JQL, not SQL, so think of it as a typed query builder over Jira search rather than a full relational ORM. That framing is the value. Querying Jira data straight from the REST API means hand-writing JQL strings, and a custom field is not `severity` in the query, it's `cf[10042]`. That id is assigned by Jira and differs in every environment, so the same query string breaks the moment you move it from dev to prod.

`Bug.c.severity == "S1"` sidesteps that. stint resolves the field id from the same state file the migrations use, so you reference the field by name and the right `cf[N]` gets filled in for whatever environment you point at. You write the expression once. It runs in dev and in prod with no hardcoded ids and no per-environment string surgery.

The query layer also routes through a dialect instead of emitting one fixed JQL string. The same expression can compile differently per backend, so the abstraction holds as new backends land without touching a single query in your code.

## Where this stands today

stint is alpha. Worth being straight about the edges.

- Jira Cloud is the target. Company-managed and team-managed projects. Data Center is out of scope, because roughly 17 of the admin endpoints stint drives exist only on Cloud and DC never shipped them over REST. Future backends like Linear come through the dialect protocol.
- No transactions. Jira admin REST doesn't offer them, so a failed migration can leave a partial state. Re-running picks up where it left off, because each operation checks what already exists first.
- Drift still happens. Someone can always open the UI and click. stint won't stop them. What it gives you is a way to reflect the live instance, see the diff, and pull the change back into code on your terms.

That last point matters. The goal isn't to lock the UI. It's to make the source of truth a repo your team reviews, instead of a memory of who clicked what.

## The point

Your Jira config decides how every team in the company files work. It's production infrastructure. It deserves the same discipline as the rest of your infrastructure: review, history, repeatable environments, a rollback path.

Right now most teams manage it with a web UI and good intentions.

stint is the attempt to give Jira admin the tooling databases got two decades ago. Schema as code. Migrations you commit. Environments that match because they ran the same chain.

If you've ever reconciled two Jira instances by hand, you already know why.

```bash
uv add stint
```
