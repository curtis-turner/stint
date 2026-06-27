"""Shared Cyclopts App for the stint CLI.

The App is created here so command modules can import it and register
subcommands with ``@app.command``. ``main(argv)`` is the entry point used
by both the console script and the test suite; it invokes the App in
``return_value`` mode so command functions returning ``int`` come back
as plain ints rather than triggering ``sys.exit``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from cyclopts import App

from stint.exceptions import StintError

app = App(
    name="stint",
    help="Declarative schema-as-code and ORM for work-management systems.",
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the command's int exit code.

    Domain errors (StintError and subclasses — transport, auth, config,
    reflection, ...) are reported as a single clean line on stderr instead of a
    Python traceback. Unexpected errors still propagate with a traceback so
    bugs stay debuggable. A migration that fails mid-apply leaves Jira in a
    partial state (no transactions); re-running resumes where it stopped.
    """
    try:
        rc = app(argv, result_action="return_value")
    except StintError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return int(rc) if rc is not None else 0
