"""End-to-end test of `pensum revision --autogenerate` CLI."""

import sys

import httpx
import pytest
import respx

from pensum import StateFile, load_migrations
from pensum.cli.main import main
from pensum.registry import registry

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.platform", None)


def _paginated(values):
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def _stub_empty_jira(mock):
    """All admin endpoints return empty — Jira has nothing."""
    mock.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "9",
                "deploymentType": "Server",
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/field").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    for path in (
        "/project/search",
        "/screens",
        "/screenscheme",
        "/issuetypescheme",
        "/issuetypescheme/mapping",
        "/issuetypescheme/project",
        "/issuetypescreenscheme",
        "/issuetypescreenscheme/project",
        "/fieldconfiguration",
        "/fieldconfigurationscheme",
        "/fieldconfigurationscheme/project",
    ):
        mock.get(f"{CLOUD_ROOT}{path}").mock(return_value=httpx.Response(200, json=_paginated([])))


@respx.mock
def test_autogenerate_greenfield_writes_full_migration(tmp_path, monkeypatch, capsys):
    """Empty state, empty Jira, schema = platform example → writes a migration
    that loads cleanly and contains the expected op calls."""
    _stub_empty_jira(respx.mock)

    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("PENSUM_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "initial platform schema",
            "--autogenerate",
            "--schema",
            "examples.platform",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "operation(s) emitted" in out

    # Re-load via the migration loader — confirms the generated file is valid.
    graph = load_migrations(mig_dir)
    assert len(graph.by_revision) == 1
    new = next(iter(graph.by_revision.values()))
    assert new.down_revision is None

    # Inspect source for sanity.
    files = list(mig_dir.glob("*.py"))
    source = files[0].read_text()
    assert "from pensum import op" in source
    assert "from pensum.fields import SelectField, TextField" in source
    assert "op.create_custom_field" in source
    assert "op.create_issuetype" in source
    assert "op.create_screen" in source
    assert "op.add_screen_tab_field" in source
    assert "op.create_screen_scheme" in source
    assert "op.create_field_configuration" in source
    assert "op.create_issuetype_scheme" in source
    assert "op.create_issuetype_screen_scheme" in source
    assert "op.create_field_configuration_scheme" in source
    assert "op.create_project" in source
    assert "op.set_project_issuetype_scheme" in source
    assert "op.set_project_issuetype_screen_scheme" in source
    assert "op.set_project_field_configuration_scheme" in source
    # Downgrade is the standard refusal:
    assert "op.unsupported(" in source


@respx.mock
def test_autogenerate_in_sync_writes_nothing(tmp_path, monkeypatch, capsys):
    """If state already matches schema (or schema is empty), no file is written."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("PENSUM_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "noop",
            "--autogenerate",
            # No schema declarations imported (registry empty due to fixture reset).
            "--schema",
            "pensum.registry",  # any importable, no schema content
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    assert "in sync" in capsys.readouterr().out
    assert not mig_dir.exists() or not list(mig_dir.glob("*.py"))


@respx.mock
def test_autogenerate_orphan_state_warns_without_allow_delete(
    tmp_path,
    monkeypatch,
    capsys,
):
    """State has an alias that schema doesn't declare; without --allow-delete,
    autogenerate emits a warning and (since schema declares nothing else) no file."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    state = StateFile(env="dev", jira_url=BASE)
    from pensum.state.file import CustomFieldMapping

    state.custom_fields["orphan"] = CustomFieldMapping(id="customfield_99999")
    state.save(state_path)

    monkeypatch.setenv("PENSUM_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "noop",
            "--autogenerate",
            "--schema",
            "pensum.registry",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning:" in out
    assert "orphan" in out
    assert "in sync" in out


@respx.mock
def test_autogenerate_allow_delete_emits_delete(tmp_path, monkeypatch, capsys):
    """Same as above but with --allow-delete: a delete op IS emitted."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    state = StateFile(env="dev", jira_url=BASE)
    from pensum.state.file import CustomFieldMapping

    state.custom_fields["orphan"] = CustomFieldMapping(id="customfield_99999")
    state.save(state_path)

    monkeypatch.setenv("PENSUM_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "drop orphan",
            "--autogenerate",
            "--schema",
            "pensum.registry",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
            "--allow-delete",
        ]
    )
    assert rc == 0
    files = list(mig_dir.glob("*.py"))
    assert len(files) == 1
    src = files[0].read_text()
    assert "op.delete_custom_field(alias='orphan')" in src
