"""`stint validate`: run schema-level checks on a Python schema module.

Imports the user's schema module (dotted path or file path), then runs
`stint.validate()` against the populated registry. Reports problems and
exits non-zero if any are found. No network calls.

ConfigurationError raised during import (metaclass checks like option-list
drift, TMP+ScreenScheme combinations) is caught and reported the same way
as registry-level problems so the user gets one consistent error surface.
"""

from __future__ import annotations

from typing import Annotated

from cyclopts import Parameter

from stint.autogen.loader import load_schema_module
from stint.cli.app import app
from stint.exceptions import ConfigurationError
from stint.validate import validate as _run_schema_validation


@app.command(name="validate")
def validate_cmd(
    *,
    schema: Annotated[
        str,
        Parameter(help="Dotted module path (e.g. examples.platform) or file path (./schemas/platform.py)."),
    ],
) -> int:
    """Run schema-level checks on a Python schema module (no network calls)."""
    try:
        load_schema_module(schema)
    except ConfigurationError as e:
        print(f"ERROR: {e}")
        return 1

    problems = _run_schema_validation()
    if problems:
        plural = "s" if len(problems) != 1 else ""
        print(f"Schema validation failed ({len(problems)} problem{plural}):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK: {schema} is valid.")
    return 0
