# examples/adf/schema.py
"""Minimal schema for the ADF integration example.

* Project:  TEST (team‑managed)
* IssueType:  Foo
"""

from __future__ import annotations

from stint import IssueType, Project

# --- Custom fields --------------------------------------------------------------


# --- Issue type ---------------------------------------------------------------
class Foo(IssueType):
    __alias__ = "foo"
    __description__ = "Sample issue type for ADF Testing"
    __screen_scheme__ = None
    __field_configuration__ = None

    # System fields
    summary: str
    description: str | None = None


# --- Project ---------------------------------------------------------------
class ADFProject(Project):
    __key__ = "TEST"
    __style__ = "team-managed"
    __issuetypes__ = [Foo]
