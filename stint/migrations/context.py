"""Migration execution context.

Bound by the runner before invoking a migration's upgrade/downgrade body.
The op functions look up the context to find the engine and state file
they should mutate.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from stint.engine import Engine
    from stint.state.file import StateFile


_context: contextvars.ContextVar[MigrationContext] = contextvars.ContextVar(
    "stint_migration_context",
)


@dataclass
class MigrationContext:
    engine: Engine
    state: StateFile
    direction: Literal["upgrade", "downgrade"]
    state_path: str | Path | None = None

    def persist(self) -> None:
        """Save the state file to disk if state_path is set. Used by multi-step
        ops to checkpoint progress before a child call that might fail."""
        if self.state_path is not None:
            self.state.save(self.state_path)


def get_context() -> MigrationContext:
    """Return the active MigrationContext. Raises LookupError outside a migration."""
    try:
        return _context.get()
    except LookupError as e:
        raise LookupError("op functions must be called from within a migration's upgrade() or downgrade() body.") from e


def set_context(ctx: MigrationContext) -> contextvars.Token:
    return _context.set(ctx)


def reset_context(token: contextvars.Token) -> None:
    _context.reset(token)
