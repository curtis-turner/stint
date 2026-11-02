"""Schema-module loader: dotted paths and file paths."""

import sys

import pytest

from stint.autogen.loader import load_schema_module


def test_dotted_module_resolves_from_cwd(tmp_path, monkeypatch):
    """A dotted schema path imports from the working directory even when cwd is
    not already on sys.path — the situation under the installed `stint` console
    script, whose sys.path[0] is the venv bin dir, not cwd."""
    (tmp_path / "mymod.py").write_text("VALUE = 42\n", encoding="utf-8")
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
    schema.write_text("NAME = 'ok'\n", encoding="utf-8")
    mod = load_schema_module(str(schema))
    assert mod.NAME == "ok"


def test_two_same_stem_files_do_not_collide(tmp_path):
    """L18: the synthetic module name used to be ``stint_schema_{stem}``, so
    two schema files named the same in different directories shared one
    sys.modules slot — loading the second evicted the first, and any later
    reload of the first re-registered its objects into a registry keyed
    against the second file's. Names are now path-keyed, so each file is its
    own declaring module and both object sets coexist."""
    from stint import registry

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "platform.py").write_text(
        "from stint import CustomField, SelectField\n"
        "CF = CustomField(alias='sev_a', name='Severity', type=SelectField, options=['S1'])\n",
        encoding="utf-8",
    )
    (dir_b / "platform.py").write_text(
        "from stint import CustomField, SelectField\n"
        "CF = CustomField(alias='sev_b', name='Bug Cause', type=SelectField, options=['S1'])\n",
        encoding="utf-8",
    )
    registry.reset()
    try:
        mod_a = load_schema_module(str(dir_a / "platform.py"))
        cf_a = registry.custom_fields["sev_a"]
        mod_b = load_schema_module(str(dir_b / "platform.py"))
        # Both files' objects registered and both module names distinct.
        assert registry.custom_fields["sev_a"] is cf_a
        assert registry.custom_fields["sev_b"]._declared_module == mod_b.__name__
        assert mod_a.__name__ != mod_b.__name__
        # And reloading file A (its own aliases) still succeeds.
        mod_a2 = load_schema_module(str(dir_a / "platform.py"))
        assert registry.custom_fields["sev_a"] is not cf_a
        assert registry.custom_fields["sev_a"]._declared_module == mod_a2.__name__
    finally:
        registry.reset()


def test_reloading_same_schema_file_replaces_without_duplicate(tmp_path):
    """L18: re-executing the same schema file re-declares its own objects
    with fresh object identities; the registry used to raise
    ``Duplicate CustomField alias`` because its dedup was an identity check."""
    from stint import registry

    schema = tmp_path / "schema.py"
    schema.write_text(
        "from stint import CustomField, SelectField\n"
        "CF = CustomField(alias='sev', name='Severity', type=SelectField, options=['S1'])\n",
        encoding="utf-8",
    )
    registry.reset()
    try:
        load_schema_module(str(schema))
        cf_first = registry.custom_fields["sev"]
        load_schema_module(str(schema))
        cf_second = registry.custom_fields["sev"]
        assert cf_first is not cf_second  # fresh execution, replaced in place
        assert registry.custom_fields["sev"] is cf_second
    finally:
        registry.reset()


def test_duplicate_alias_from_a_different_module_still_raises(tmp_path):
    """The reload-replace must not weaken the duplicate guard: declaring the
    same alias from two different schema modules is still an error."""
    from stint import registry
    from stint.exceptions import RegistryError

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    body = (
        "from stint import CustomField\n"
        "from stint import SelectField\n"
        "CF = CustomField(alias='sev', name='Severity', type=SelectField, options=['S1'])\n"
    )
    (dir_a / "alpha.py").write_text(body, encoding="utf-8")
    (dir_b / "beta.py").write_text(body, encoding="utf-8")
    registry.reset()
    try:
        load_schema_module(str(dir_a / "alpha.py"))
        with pytest.raises(RegistryError, match="Duplicate CustomField"):
            load_schema_module(str(dir_b / "beta.py"))
    finally:
        registry.reset()
