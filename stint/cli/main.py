"""stint CLI dispatcher.

Importing the modules below registers every ``@app.command`` with the CLI's
Typer-like app. Note where commands live (L24): ``upgrade``,
``downgrade``, ``current`` and ``history`` are all registered by
``cmd_upgrade`` (they share its migration/state plumbing), not by
modules named after them.

  reflect    - reflect a Jira instance into a snapshot (read-only)  [cmd_reflect]
  revision   - create a new migration (empty, --merge, or --autogenerate)  [cmd_revision]
  stamp      - brownfield: match aliases against an existing Jira and populate state  [cmd_stamp]
  upgrade    - run pending migrations against an env  [cmd_upgrade]
  downgrade  - roll back to a prior revision  [cmd_upgrade]
  apply      - reflect + diff + write in one run, for reconcile-style dialects (jira_cloud_tmp)  [cmd_apply]
  current    - show the current migration revision recorded in a state file  [cmd_upgrade]
  history    - list all migrations in revision order  [cmd_upgrade]
  validate   - run stint.validate() on a Python schema module (no network calls)  [cmd_validate]
"""

from __future__ import annotations

import sys

from stint.cli import (  # noqa: F401 — side-effect: registers @app.command
    cmd_apply,
    cmd_reflect,
    cmd_revision,
    cmd_stamp,
    cmd_upgrade,
    cmd_validate,
)
from stint.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
