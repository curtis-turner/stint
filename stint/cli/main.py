"""stint CLI dispatcher.

Subcommands:
  reflect    - reflect a Jira instance into a snapshot (read-only)
  revision   - create a new migration (empty, --merge, or --autogenerate)
  stamp      - brownfield: match aliases against an existing Jira and populate state
  upgrade    - run pending migrations against an env
  downgrade  - roll back to a prior revision
  current    - show the current migration revision recorded in a state file
  history    - list all migrations in revision order
  validate   - run stint.validate() on a Python schema module (no network calls)
"""

from __future__ import annotations

import sys

from stint.cli import (  # noqa: F401 — side-effect: registers @app.command
    cmd_reflect,
    cmd_revision,
    cmd_stamp,
    cmd_upgrade,
    cmd_validate,
)
from stint.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
