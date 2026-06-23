"""Migration runner. Walks the revision graph and executes each migration's
upgrade/downgrade body, persisting the state file after each successful step.

Failure handling: a migration that raises mid-body leaves the state file at
the LAST successful revision. The partially-applied migration's already-issued
API calls are NOT rolled back (Jira admin REST has no transactions). Re-running
upgrade will re-execute the failed migration from the top; ops must therefore
either be idempotent or fail loudly on duplicate (the alias-already-in-state
check in op functions handles the latter).

Downgrade is best-effort. UnsupportedDowngradeError aborts cleanly without
mutating state.revision; the migration that raised it is still considered
applied. Authors guard data-destroying downgrades with op.unsupported().
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from stint.engine import Engine
from stint.migrations.base import Migration, RevisionGraph
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.state.file import StateFile


async def upgrade(
    engine: Engine,
    state: StateFile,
    graph: RevisionGraph,
    state_path: str | Path,
    *,
    target: str | None = None,
) -> list[Migration]:
    """Apply migrations from state.revision → target (or head if target is None).

    Returns the list of migrations that ran. Empty list means already at target.
    Persists state file after each successful migration.
    """
    pending = graph.chain_from(state.revision)
    if target is not None:
        pending = _truncate_to_target(pending, target)
    applied: list[Migration] = []
    for migration in pending:
        await _run_one(
            engine,
            state,
            migration,
            direction="upgrade",
            state_path=state_path,
        )
        state.revision = migration.revision
        state.last_applied = _now_iso()
        state.save(state_path)
        applied.append(migration)
    return applied


def _truncate_to_target(chain: list[Migration], target: str) -> list[Migration]:
    out: list[Migration] = []
    for m in chain:
        out.append(m)
        if m.revision == target:
            return out
    raise ValueError(f"target revision {target!r} is not in the pending chain ({[m.revision for m in chain]})")


async def downgrade(
    engine: Engine,
    state: StateFile,
    graph: RevisionGraph,
    state_path: str | Path,
    *,
    target: str | None,
) -> list[Migration]:
    """Roll back from state.revision → target (None means all the way to base).

    Walks backward through the chain. After each migration's downgrade body
    runs, state.revision is set to that migration's parent (down_revision),
    which may be None at the base. State is persisted after each step.

    target must be a revision that is an ancestor of state.revision, or None.
    Returns the migrations that were reversed, in the order they ran.
    """
    if state.revision is None:
        return []
    if target == state.revision:
        return []
    chain = _build_downgrade_chain(graph, state.revision, target)
    reversed_migrations: list[Migration] = []
    for migration in chain:
        await _run_one(
            engine,
            state,
            migration,
            direction="downgrade",
            state_path=state_path,
        )
        # For merge migrations (tuple parents), pick the first parent. The
        # other branch's migrations remain in Jira but are unreachable from
        # state.revision until upgrade re-walks them. Documented limitation.
        state.revision = _first_parent(migration)
        state.last_applied = _now_iso()
        state.save(state_path)
        reversed_migrations.append(migration)
    return reversed_migrations


def _first_parent(migration: Migration) -> str | None:
    parents = migration.parents()
    return parents[0] if parents else None


def _build_downgrade_chain(
    graph: RevisionGraph,
    current: str,
    target: str | None,
) -> list[Migration]:
    """Walk from current back toward target, returning migrations in
    head→base order. Each one's downgrade() will be called in turn."""
    if current not in graph.by_revision:
        from stint.migrations.exceptions import MigrationGraphError

        raise MigrationGraphError(f"current revision {current!r} is not in the migration graph")
    chain: list[Migration] = []
    m: Migration | None = graph.by_revision[current]
    while m is not None:
        chain.append(m)
        parent = _first_parent(m)
        if parent == target:
            return chain
        if parent is None:
            if target is not None:
                from stint.migrations.exceptions import MigrationGraphError

                raise MigrationGraphError(
                    f"target revision {target!r} is not an ancestor of current revision {current!r}"
                )
            return chain
        m = graph.by_revision.get(parent)
    return chain


async def _run_one(
    engine: Engine,
    state: StateFile,
    migration: Migration,
    *,
    direction: str,
    state_path: str | Path | None = None,
) -> None:
    ctx = MigrationContext(
        engine=engine,
        state=state,
        direction=direction,  # type: ignore[arg-type]
        state_path=state_path,
    )
    token = set_context(ctx)
    try:
        body = migration.upgrade if direction == "upgrade" else migration.downgrade
        await body()
    finally:
        reset_context(token)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
