"""Migration model and revision graph.

A migration is a Python module with four module-level globals:
  - revision: str (unique identifier)
  - down_revision: str | None (parent revision, or None for the base)
  - upgrade: async () -> None
  - downgrade: async () -> None

The loader parses these into Migration instances. The runner walks the
resulting graph and executes upgrade/downgrade bodies via the op API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pensum.migrations.exceptions import MigrationConflictError, MigrationGraphError


@dataclass(frozen=True)
class Migration:
    """One migration unit. Loaded from a Python file.

    `down_revision` is:
      - None for the base
      - a str for a normal single-parent migration
      - a tuple[str, ...] for a merge migration (joins two or more branches)
    """

    revision: str
    down_revision: str | tuple[str, ...] | None
    description: str
    source_path: str
    upgrade: Callable[[], Awaitable[None]]
    downgrade: Callable[[], Awaitable[None]]

    def parents(self) -> tuple[str, ...]:
        """Normalize down_revision into a parents tuple. () for the base."""
        dr = self.down_revision
        if dr is None:
            return ()
        if isinstance(dr, str):
            return (dr,)
        return tuple(dr)


@dataclass
class RevisionGraph:
    """DAG of migrations, keyed by revision. Supports single-parent and merge nodes."""

    by_revision: dict[str, Migration]

    @classmethod
    def from_migrations(cls, migrations: list[Migration]) -> RevisionGraph:
        by_revision: dict[str, Migration] = {}
        for m in migrations:
            if m.revision in by_revision:
                raise MigrationConflictError(
                    f"two migrations share revision {m.revision!r}: "
                    f"{by_revision[m.revision].source_path} and {m.source_path}"
                )
            by_revision[m.revision] = m
        # Validate parents exist
        for m in migrations:
            for parent in m.parents():
                if parent not in by_revision:
                    raise MigrationGraphError(
                        f"migration {m.revision!r} ({m.source_path}) has "
                        f"down_revision parent {parent!r} which does not exist"
                    )
        return cls(by_revision=by_revision)

    def bases(self) -> list[Migration]:
        """Migrations with no parent. Usually exactly one (the initial migration)."""
        return [m for m in self.by_revision.values() if not m.parents()]

    def heads(self) -> list[Migration]:
        """Migrations with no children. Usually exactly one. >1 means parallel branches."""
        all_parents: set[str] = set()
        for m in self.by_revision.values():
            all_parents.update(m.parents())
        return [m for r, m in self.by_revision.items() if r not in all_parents]

    def chain_from(self, current: str | None) -> list[Migration]:
        """Return migrations to apply, in order, to go from current → single head.

        Walks the DAG topologically. At each step, picks the unique unapplied
        migration whose parents are all applied. Errors if multiple heads exist
        (resolve with a merge migration) or if a fork without merge produces
        multiple unblocked candidates.
        """
        heads = self.heads()
        if not heads:
            return []
        if len(heads) > 1:
            head_revs = sorted(h.revision for h in heads)
            raise MigrationGraphError(
                f"multiple heads exist: {head_revs}. Run 'pensum revision --merge <head1> <head2> -m ...' to merge."
            )
        if current is not None and current not in self.by_revision:
            raise MigrationGraphError(
                f"current revision {current!r} is not in the migration graph. "
                f"State file may be from a different repo or out of sync."
            )

        applied: set[str] = set()
        if current is not None:
            applied = self._ancestors(current) | {current}

        chain: list[Migration] = []
        remaining = {r: m for r, m in self.by_revision.items() if r not in applied}
        while remaining:
            ready = [m for m in remaining.values() if all(p in applied for p in m.parents())]
            if not ready:
                # Should be unreachable given the head-count check above.
                raise MigrationGraphError(
                    f"deadlock walking migration graph from {current!r}; remaining={sorted(remaining)}"
                )
            # If multiple are ready, both branches of a fork are unblocked.
            # That's expected when a merge migration sits ahead — heads()==1
            # already proved everything eventually rejoins. Pick deterministically
            # by revision id so runs are reproducible.
            ready.sort(key=lambda m: m.revision)
            m = ready[0]
            chain.append(m)
            applied.add(m.revision)
            del remaining[m.revision]
        return chain

    def all_ordered(self) -> list[Migration]:
        """All migrations from base → head, for `pensum history`. Assumes single head."""
        return self.chain_from(None)

    def _ancestors(self, revision: str) -> set[str]:
        """All revisions reachable by walking parents from the given revision."""
        seen: set[str] = set()
        stack = [revision]
        while stack:
            r = stack.pop()
            m = self.by_revision.get(r)
            if m is None:
                continue
            for p in m.parents():
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen
