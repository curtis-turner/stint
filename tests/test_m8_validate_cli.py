"""`stint validate` CLI: schema-level checks without network calls.

Covers the success path against the bundled example, the failure paths for
both registry-level problems and metaclass-time ConfigurationError raised
during import, and the unknown-module error.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stint import registry
from stint.cli.main import main


@pytest.fixture(autouse=True)
def _reset_registry():
    registry.reset()
    yield
    registry.reset()


def _write_schema(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "schema.py"
    p.write_text(textwrap.dedent(body))
    return p


def test_validate_clean_schema_exits_zero(capsys):
    rc = main(["validate", "--schema", "examples.platform"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert "examples.platform" in out


def test_validate_by_file_path_works(tmp_path, capsys):
    schema = _write_schema(
        tmp_path,
        """
        from typing import Annotated, Literal

        from stint import (
            CustomField, IssueType, Project, Screen, ScreenScheme,
            FieldConfiguration, SelectField,
        )

        sev = CustomField(
            alias="sev", name="Sev", type=SelectField, options=["A", "B"],
        )
        create = Screen(alias="s_create", name="Create", fields=["Summary", sev])
        edit = Screen(alias="s_edit", name="Edit", fields=["Summary", sev])
        view = Screen(alias="s_view", name="View", fields=["Summary", sev])
        scheme = ScreenScheme(
            alias="ss", name="Scheme", create=create, edit=edit, view=view,
        )
        fc = FieldConfiguration(alias="fc", name="FC", required=["Summary"])

        class Thing(IssueType):
            __alias__ = "thing"
            __screen_scheme__ = scheme
            __field_configuration__ = fc

            summary: str
            sev: Annotated[Literal["A", "B"], sev]

        class Proj(Project):
            __key__ = "PRJ"
            __lead__ = "x"
            __style__ = "company-managed"
            __issuetypes__ = [Thing]
        """,
    )
    rc = main(["validate", "--schema", str(schema)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


def test_validate_reports_registry_inconsistency(tmp_path, capsys):
    # Screen references a CustomField that is constructed but then mutated
    # to point at an alias not in the registry. Simulate by hand-injecting
    # an unregistered CustomField reference into a Screen.
    schema = _write_schema(
        tmp_path,
        """
        from stint import CustomField, Screen, SelectField
        from stint.registry import registry

        real = CustomField(alias="real", name="Real", type=SelectField, options=["A"])
        screen = Screen(alias="scr", name="Scr", fields=["Summary", real])
        # Drop the custom field from the registry so the Screen reference dangles.
        registry.custom_fields.pop("real")
        """,
    )
    rc = main(["validate", "--schema", str(schema)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Schema validation failed" in out
    assert "'real'" in out


def test_validate_surfaces_metaclass_configuration_error(tmp_path, capsys):
    # TMP project that references a ScreenScheme should be rejected by the
    # Project metaclass at class-definition time.
    schema = _write_schema(
        tmp_path,
        """
        from typing import Annotated, Literal

        from stint import (
            CustomField, IssueType, Project, Screen, ScreenScheme,
            FieldConfiguration, SelectField,
        )

        sev = CustomField(
            alias="sev", name="Sev", type=SelectField, options=["A"],
        )
        create = Screen(alias="s_c", name="C", fields=["Summary", sev])
        edit = Screen(alias="s_e", name="E", fields=["Summary", sev])
        view = Screen(alias="s_v", name="V", fields=["Summary", sev])
        scheme = ScreenScheme(
            alias="ss", name="Scheme", create=create, edit=edit, view=view,
        )
        fc = FieldConfiguration(alias="fc", name="FC", required=["Summary"])

        class Thing(IssueType):
            __alias__ = "thing"
            __screen_scheme__ = scheme
            __field_configuration__ = fc

            summary: str
            sev: Annotated[Literal["A"], sev]

        class Proj(Project):
            __key__ = "PRJ"
            __lead__ = "x"
            __style__ = "team-managed"
            __issuetypes__ = [Thing]
        """,
    )
    rc = main(["validate", "--schema", str(schema)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out


def test_validate_unknown_module_exits_one(capsys):
    rc = main(["validate", "--schema", "this.module.does.not.exist"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out
