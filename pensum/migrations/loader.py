"""Load Migration objects from a directory of Python files.

Each file is a normal Python module. Loader imports it with importlib.util
(not via the regular module path) and pulls out the four required globals.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pensum.migrations.base import Migration, RevisionGraph
from pensum.migrations.exceptions import MigrationError

REQUIRED_GLOBALS = ("revision", "down_revision", "upgrade", "downgrade")


def load_migrations(directory: str | Path) -> RevisionGraph:
    """Scan a directory for migration files and return the revision graph."""
    d = Path(directory)
    if not d.is_dir():
        raise MigrationError(f"migrations directory does not exist: {d}")
    migrations: list[Migration] = []
    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
        migrations.append(_load_migration_file(path))
    return RevisionGraph.from_migrations(migrations)


def _load_migration_file(path: Path) -> Migration:
    spec = importlib.util.spec_from_file_location(f"pensum_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"cannot import migration file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in REQUIRED_GLOBALS:
        if not hasattr(module, name):
            raise MigrationError(f"migration {path} is missing required global {name!r}")
    description = getattr(module, "description", "") or _extract_description(path)
    dr = module.down_revision
    if dr is not None and not isinstance(dr, (str, tuple)):
        raise MigrationError(
            f"migration {path}: down_revision must be None, str, or tuple of str (got {type(dr).__name__})"
        )
    return Migration(
        revision=module.revision,
        down_revision=dr,
        description=description,
        source_path=str(path),
        upgrade=module.upgrade,
        downgrade=module.downgrade,
    )


def _extract_description(path: Path) -> str:
    """Best-effort: derive description from filename `YYYY_MM_DD_HHMM_slug.py`."""
    stem = path.stem
    parts = stem.split("_")
    if len(parts) >= 5:
        return " ".join(parts[4:]).replace("_", " ")
    return stem
