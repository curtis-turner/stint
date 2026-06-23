"""Migration system (Alembic-style) for stint.

Public API:
  - Migration: declarative migration unit with revision, down_revision, upgrade, downgrade
  - op: namespace for the operations a migration can perform
  - get_context: current migration context (engine + state) inside an upgrade/downgrade

CLI surface:
  - stint upgrade --env <name>
  - stint current --env <name>
  - stint history
  - stint revision --autogenerate -m "..."   (deferred to next slice)
  - stint downgrade --env <name> -r ...       (deferred to next slice)
"""

from stint.migrations import op
from stint.migrations.base import Migration
from stint.migrations.context import MigrationContext, get_context
from stint.migrations.exceptions import (
    MigrationConflictError,
    MigrationError,
    MigrationGraphError,
    UnsupportedDowngradeError,
)
from stint.migrations.loader import load_migrations
from stint.migrations.runner import downgrade, upgrade

__all__ = [
    "Migration",
    "MigrationConflictError",
    "MigrationContext",
    "MigrationError",
    "MigrationGraphError",
    "UnsupportedDowngradeError",
    "downgrade",
    "get_context",
    "load_migrations",
    "op",
    "upgrade",
]
