"""Schema-module loader: dotted paths and file paths."""

import sys

from stint.autogen.loader import load_schema_module


def test_dotted_module_resolves_from_cwd(tmp_path, monkeypatch):
    """A dotted schema path imports from the working directory even when cwd is
    not already on sys.path — the situation under the installed `stint` console
    script, whose sys.path[0] is the venv bin dir, not cwd."""
    (tmp_path / "mymod.py").write_text("VALUE = 42\n")
    monkeypatch.chdir(tmp_path)
    # Simulate the console-script invocation: cwd absent from sys.path.
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in (str(tmp_path), "")])
    sys.modules.pop("mymod", None)
    try:
        mod = load_schema_module("mymod")
        assert mod.VALUE == 42
        assert str(tmp_path) in sys.path
    finally:
        sys.modules.pop("mymod", None)


def test_file_path_resolves(tmp_path):
    """A filesystem path imports directly without touching sys.path."""
    schema = tmp_path / "schema.py"
    schema.write_text("NAME = 'ok'\n")
    mod = load_schema_module(str(schema))
    assert mod.NAME == "ok"
