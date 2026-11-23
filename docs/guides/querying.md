# Querying and writing (the ORM)

The **data plane** reads and writes *issues* using the same model classes declared in the [schema plane](schema.md). Sessions are introduced through an [`Engine`](../api/engine.md) plus a loaded [`StateFile`](../api/state.md); the state's alias → id maps are what translate model attributes into `cf[<id>]` payloads and `where` clauses into JQL.

## Sessions

Sync and async flavours share the same surface; async adds `await`:

```python
from stint.query import AsyncSession, Session

async with AsyncSession(engine, state) as session:  # or Session(...)
    ...
```

Both provide an **identity map** (one instance per row per session) and **dirty tracking** (an update sends only changed fields). `commit()` returns— or raises —a [`CommitResult`](../api/query.md) batch.

## Creating

```python
bug = Bug(summary="Crash on save", severity="S1")
session.add(bug)  # project inferred if Bug belongs to exactly one
session.add(bug, project="SEC")  # or pass the key explicitly

results = await session.commit()
print(bug.key)  # populated after a successful insert,
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

s1_bugs = await session.scalars(select(Bug).where(Bug.c.severity == "S1").order_by(Bug.c.created, "DESC").limit(10))
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
await session.commit()  # PUT carries only `severity`

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

## Workflow transitions

Status changes are not field writes; they move the issue through the project's workflow:

```python
bug = await session.get(Bug, "PLAT-123")
await session.transition(bug, to_status="Done", resolution="Fixed")
```

- `to_status=` is matched **case-insensitively** against the target status of each available transition. A raw transition id works too, via `transition_id=`.
- Transitioning to the issue's **current status is a no-op**, so a sync loop that re-derives the desired state every pass stays idempotent.
- `resolution=` and `fields={...}` (raw Jira JSON for transition-screen fields) ride in the same POST as the transition.
- When no transition leads to the requested status, stint raises [`TransitionError`](../api/exceptions.md) naming the statuses that ARE reachable.
- `available_transitions(issue)` lists what the workflow allows right now; each entry unpacks as `(id, name, to_status)`:

```python
for tid, name, to_status in await session.available_transitions(bug):
    print(tid, name, "->", to_status)
```

Models can declare the read-only `status_category` attribute alongside `status`. It is hydrated from the status payload's category (`"new"`, `"indeterminate"` or `"done"`), so callers can ask "is it done?" without hardcoding each workflow's status names:

```python
class Bug(IssueType):
    __alias__ = "bug"
    summary: str
    status: str | None = None
    status_category: str | None = None  # "new" / "indeterminate" / "done"


if bug.status_category == "done":
    ...
```

After a successful transition, a hydrated instance's `status`/`status_category` are refreshed in place and excluded from dirty tracking, so a later `commit()` does not PUT the old status back.

## Comments

```python
await session.add_comment(bug, "Validation passed, upstream record is in sync.")
comments = await session.comments(bug)
await session.delete_comment(bug, comments[0].id)
```

- Plain-text bodies are wrapped in ADF on Cloud (reusing `stint.query.adf`); a dict body is taken as raw ADF and sent untouched.
- `comments()` paginates and renders every body to plain text (`Comment(id, author, created, body_text)`), so a sync loop can deduplicate: read first, only post when the message is not already there.

```python
existing = {c.body_text for c in await session.comments(bug)}
if "nag text" not in existing:
    await session.add_comment(bug, "nag text")
```

## Attachments

```python
await session.attach(bug, "export.csv")  # path; name inferred
await session.attach(bug, raw_bytes, filename="export.csv")

files = await session.attachments(bug)  # [Attachment(id, filename, size, created)]
data = await session.download_attachment(files[0].id)
await session.delete_attachment(files[0].id)
```

- Uploads are multipart with the required `X-Atlassian-Token: no-check` header; size limits and server errors surface as `StintError` subclasses (e.g. a 413 arrives as `TransportError` with `status_code=413`).
- The regenerated-export pattern — delete the old copy, upload the new one — is a helper:

```python
await session.replace_attachment(bug, "export.csv")  # deletes same-named files, then uploads
```

## System fields: duedate and parent

Beyond `summary`, `description`, `priority`, `reporter` and `assignee`, models can declare the typed system fields `duedate` and `parent`:

```python
from datetime import date


class WorkItem(IssueType):
    __alias__ = "workitem"
    summary: str
    duedate: date | None = None  # system "duedate", sent as YYYY-MM-DD


class FollowUp(IssueType):  # a sub-task type
    __alias__ = "followup"
    __subtask__ = True
    summary: str
    parent: str | None = None  # parent issue key, e.g. "PLAT-9"
```

Behavior:

- `duedate` is sent as `YYYY-MM-DD`; setting it to `None` on update clears it (sends `null`), and it's queryable in JQL.
- `parent` is sent as `{"key": ...}` and hydrated back as the parent's key. It also works for moving a sub-task to a new parent on update.
- A model with `__subtask__ = True` must carry a `parent` on insert; stint raises a `ConfigurationError` before the request is sent. Types without the flag treat `parent` as an ordinary optional field.

## Other system fields: the passthrough

The payload and hydration layers pass any unrecognized attribute name through to Jira as a system field of the same name. Fields stint knows the JSON shapes of are translated (selects, users, dates, parent, ...); bare-scalar system fields such as `environment` work as plain attributes without needing a stint release:

```python
class Patch(IssueType):
    __alias__ = "patch"
    summary: str
    environment: str | None = None  # sent as-is, read back as-is
```

Object-shaped system fields (`components`, `fixVersions`) also pass through, but the caller supplies the raw Jira JSON shape (e.g. `[{"name": "Backend"}]`) and gets it back the same way; those may gain typed support later. Check the create/update screen carries the field, or Jira will reject the request.

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
