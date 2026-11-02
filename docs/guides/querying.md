# Querying and writing (the ORM)

The **data plane** reads and writes *issues* using the same model classes declared in the [schema plane](schema.md). Sessions are introduced through an [`Engine`](../api/engine.md) plus a loaded [`StateFile`](../api/state.md); the state's alias → id maps are what translate model attributes into `cf[<id>]` payloads and `where` clauses into JQL.

## Sessions

Sync and async flavours share the same surface; async adds `await`:

```python
from stint.query import AsyncSession, Session

async with AsyncSession(engine, state) as session:   # or Session(...)
    ...
```

Both provide an **identity map** (one instance per row per session) and **dirty tracking** (an update sends only changed fields). `commit()` returns— or raises —a [`CommitResult`](../api/query.md) batch.

## Creating

```python
bug = Bug(summary="Crash on save", severity="S1")
session.add(bug)                # project inferred if Bug belongs to exactly one
session.add(bug, project="SEC") # or pass the key explicitly

results = await session.commit()
print(bug.key)                  # populated after a successful insert,
                                # e.g.  PLAT-1234
```

!!! note "reporter on create"
    Leave `reporter` unset when inserting: Jira derives it from the API token's identity, and sending it in the create payload 400s. The ORM write path takes a raw `accountId` for `reporter`/`assignee` on Cloud.

## Reading one issue

```python
first = await session.get(Bug, "PLAT-1234", fresh=True)
```

`get()` hydrates custom fields through the state maps (your typed attributes land as their declared Python types). By default hits are served from the identity map; `fresh=True` bypasses it — needed whenever Jira-side security or drift might make the served identity stale, e.g. right after an insert under an issue-security scheme that hides the issue.

## Querying with JQL

`select(Bug)` begins a buildable query; run it with `session.scalars()`:

```python
from stint.query import or_, select

s1_bugs = await session.scalars(
    select(Bug)
    .where(Bug.c.severity == "S1")
    .order_by(Bug.c.created, "DESC")
    .limit(10)
)
```

`Model.c.<attr>` gives a [`Column`](../api/query.md) supporting the comparison operators plus `in_`, `not_in`, `contains`, `is_null`, `is_not_null`. Predicates combine with `and_` / `or_` / `not_` (or the operators `&`, `|`, `~`):

```python
urgent = select(Bug).where(
    or_(
        Bug.c.severity.in_(["S1", "S2"]),
        and_(Bug.c.root_cause.is_not_null(), not_(Bug.c.severity == "S4")),
    )
)
```

Use `stmt.compile(state)` to peek at the rendered JQL (`cf[10042] = "S1"`).

## Updating and deleting

```python
bug.severity = "S2"
await session.commit()          # PUT carries only `severity`

session.delete(bug)
await session.commit()
```

## Partial commits: the honest failure mode

Jira admin operations are not transactional, and the issue write path commits a **batch** of operations at once. If *any* operation in a commit fails, stint raises [`PartialCommitError`](../api/exceptions.md) — **successful work is not rolled back** (there's no transaction to roll back to). The exception carries `results`, a `CommitResult` per operation:

```python
from stint.query.session import PartialCommitError

try:
    results = await session.commit()
except PartialCommitError as exc:
    for result in exc.results:
        if result.error is not None:
            print(f"{result.operation} FAILED: {result.error}")
    raise
```

Each result also names the operation kind and the affected instance, so you can reconcile manually and re-commit the survivors.

## Descriptions: Atlassian Document Format

Jira descriptions are ADF documents, not plain text. The [`stint.query.adf`](../api/query.md) module wraps plain text going in and parses it coming back:

```python
from stint.query import adf

bug.description = adf.wrap_plain_text("First line\nSecond line")
...
text = adf.parse_text(adf)  # read back as plain text
```

A runnable ADF walkthrough lives in [`examples/adf/`](https://github.com/curtis-turner/stint/tree/main/examples/adf), including round-tripping generated ADF JSON.

## Sync vs async

| | `Session` | `AsyncSession` |
|---|---|---|
| Query | `session.scalars(select(Bug).where(...))` | `await session.scalars(...)` |
| `get` | `session.get(Bug, key)` | `await session.get(...)` |
| `add` / `delete` | same | same |
| `commit` | `session.commit()` | `await session.commit()` |
| Context manager | `with` | `async with` |
