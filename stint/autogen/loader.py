"""Import a user's schema module by dotted path or file path.

The act of import triggers the class metaclasses and dataclass __post_init__
hooks, which register the schema objects with `stint.registry`.

The registry is process-wide. Reloading a module that's already been imported
re-runs registration; the registry's de-dup logic (`is` check on the same
object) handles that gracefully.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from stint.exceptions import ConfigurationError


def load_schema_module(target: str) -> ModuleType:
    """Import `target` as a Python module.

    `target` may be:
      - a dotted module path: "examples.platform"
      - a filesystem path: "./schemas/platform.py"
    """
    p = Path(target)
    if p.exists() and p.is_file():
        return _import_file(p)
    try:
        return importlib.import_module(target)
    except ImportError as e:
        raise ConfigurationError(f"cannot import schema module {target!r}: {e}") from e


def _import_file(path: Path) -> ModuleType:
    module_name = f"stint_schema_{path.stem}"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f"cannot import schema file {path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
