"""Import a user's schema module by dotted path or file path.

The act of import triggers the class metaclasses and dataclass __post_init__
hooks, which register the schema objects with `stint.registry`.

The registry is process-wide. Importing a file path re-executes its module
under a name derived from the resolved path (L18), so a re-run of the same
file is visible to the registry as a reload: the declaring module re-opens
its own aliases and the registry replaces them in place. Declaring the same
alias from a *different* module is still a hard error.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from stint.exceptions import ConfigurationError


def load_schema_module(target: str) -> ModuleType:
    """Import `target` as a Python module.

    `target` may be:
      - a dotted module path: "examples.company_managed.platform"
      - a filesystem path: "./schemas/platform.py"
    """
    p = Path(target)
    if p.exists() and p.is_file():
        return _import_file(p)
    # The installed `stint` console script puts the venv bin dir on sys.path[0],
    # not the working directory, so a dotted path like "schemas.platform" fails
    # to import even when run from the project root. Add cwd so dotted schema
    # paths resolve the way the docs show them.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        return importlib.import_module(target)
    except ImportError as e:
        raise ConfigurationError(f"cannot import schema module {target!r}: {e}") from e


def _import_file(path: Path) -> ModuleType:
    # Key the synthetic module name on the full resolved path, not just the
    # stem (L18): two schema files named platform.py in different directories
    # would otherwise share one sys.modules slot, so loading the second evicted
    # the first (its registered objects then collided with the re-registration
    # of the first on a later reload). The path hash is stable across reloads,
    # so re-executing the same file lands in the same module name — which the
    # registry uses to treat re-registration as a reload, not a duplicate.
    resolved = path.resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    module_name = f"stint_schema_{path.stem}_{digest}"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f"cannot import schema file {path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
