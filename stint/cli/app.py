"""Shared Cyclopts App for the stint CLI.

The App is created here so command modules can import it and register
subcommands with ``@app.command``. ``main(argv)`` is the entry point used
by both the console script and the test suite; it invokes the App in
``return_value`` mode so command functions returning ``int`` come back
as plain ints rather than triggering ``sys.exit``.
"""

from __future__ import annotations

from collections.abc import Sequence

from cyclopts import App

app = App(
    name="stint",
    help="Declarative schema-as-code and ORM for work-management systems.",
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the command's int exit code."""
    rc = app(argv, result_action="return_value")
    return int(rc) if rc is not None else 0
