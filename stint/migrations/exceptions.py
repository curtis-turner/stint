"""Migration-specific errors."""

from stint.exceptions import StintError


class MigrationError(StintError):
    """Base for migration-system errors."""


class MigrationGraphError(MigrationError):
    """Revision graph is malformed: missing parent, broken chain, etc."""


class MigrationConflictError(MigrationError):
    """Two migrations share the same revision id, or multiple heads exist."""


class UnsupportedDowngradeError(MigrationError):
    """Migration's downgrade() called op.unsupported(...).

    Raised by op.unsupported when a downgrade would destroy data. The author of
    the migration can edit the file to remove the guard if they actually want
    to roll back.
    """
