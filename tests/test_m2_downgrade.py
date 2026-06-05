"""Migration runner: downgrade path. Plus CLI `pensum downgrade` smoke."""

from pathlib import Path

import httpx
import pytest
import respx

from pensum import (
    PATAuth,
    StateFile,
    create_engine,
    load_migrations,
)
from pensum.cli.main import main
from pensum.migrations.exceptions import (
    MigrationGraphError,
    UnsupportedDowngradeError,
)
from pensum.migrations.runner import downgrade as run_downgrade
from pensum.state.file import CustomFieldMapping

BASE = "https://jira.example.com"
DC_ROOT = f"{BASE}/rest/api/2"

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "migrations"


def _engine():
    return create_engine(f"jira_dc+{BASE}", auth=PATAuth("tok"))


# ── Happy path: downgrade one step ────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_downgrade_one_step_calls_downgrade_and_updates_revision(tmp_path):
    """Currently at def789… (head). Downgrade -> abc123… runs the second
    migration's downgrade body (delete_custom_field) and bumps revision back."""
    respx.delete(f"{DC_ROOT}/field/customfield_10043").mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE, revision="def789ghi012")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(id="customfield_10043")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)

    engine = _engine()
    try:
        reversed_migrations = await run_downgrade(
            engine,
            state,
            graph,
            state_path,
            target="abc123def456",
        )
    finally:
        await engine.close()

    assert [m.revision for m in reversed_migrations] == ["def789ghi012"]
    assert state.revision == "abc123def456"
    assert "bug_root_cause" not in state.custom_fields
    assert state.custom_fields["bug_severity"].id == "customfield_10042"


# ── Persistence per step ──────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_downgrade_persists_state_after_each_step(tmp_path):
    respx.delete(f"{DC_ROOT}/field/customfield_10043").mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE, revision="def789ghi012")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(id="customfield_10043")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)

    engine = _engine()
    try:
        await run_downgrade(engine, state, graph, state_path, target="abc123def456")
    finally:
        await engine.close()

    on_disk = StateFile.load(state_path)
    assert on_disk.revision == "abc123def456"
    assert "bug_root_cause" not in on_disk.custom_fields


# ── op.unsupported aborts cleanly ─────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_downgrade_to_base_aborts_when_unsupported(tmp_path):
    """Going all the way to base hits the initial migration whose downgrade
    calls op.unsupported. The runner must abort with UnsupportedDowngradeError.
    The first downgrade step (def789…→abc123…) still succeeded and persists."""
    respx.delete(f"{DC_ROOT}/field/customfield_10043").mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE, revision="def789ghi012")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(id="customfield_10043")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)

    engine = _engine()
    try:
        with pytest.raises(UnsupportedDowngradeError) as e:
            await run_downgrade(engine, state, graph, state_path, target=None)
    finally:
        await engine.close()
    assert "bug_severity" in str(e.value)

    # State should be at the last good revision, not at base.
    assert state.revision == "abc123def456"
    on_disk = StateFile.load(state_path)
    assert on_disk.revision == "abc123def456"


# ── Noop cases ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_downgrade_at_base_is_noop(tmp_path):
    state = StateFile(env="dev", jira_url=BASE, revision=None)
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)
    engine = _engine()
    try:
        reversed_migrations = await run_downgrade(
            engine,
            state,
            graph,
            state_path,
            target=None,
        )
    finally:
        await engine.close()
    assert reversed_migrations == []


@pytest.mark.asyncio
async def test_downgrade_target_equals_current_is_noop(tmp_path):
    state = StateFile(env="dev", jira_url=BASE, revision="abc123def456")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)
    engine = _engine()
    try:
        reversed_migrations = await run_downgrade(
            engine,
            state,
            graph,
            state_path,
            target="abc123def456",
        )
    finally:
        await engine.close()
    assert reversed_migrations == []


# ── Unreachable target ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_downgrade_target_not_an_ancestor_raises(tmp_path):
    """Target that isn't in the ancestor chain must error before any HTTP."""
    state = StateFile(env="dev", jira_url=BASE, revision="abc123def456")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)
    engine = _engine()
    try:
        with pytest.raises(MigrationGraphError):
            await run_downgrade(
                engine,
                state,
                graph,
                state_path,
                target="unknown_rev",
            )
    finally:
        await engine.close()


# ── CLI smoke ─────────────────────────────────────────────────────────
@respx.mock
def test_cli_downgrade_one_step(tmp_path, monkeypatch, capsys):
    respx.delete(f"{DC_ROOT}/field/customfield_10043").mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE, revision="def789ghi012")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(id="customfield_10043")
    state_path = tmp_path / "state.yaml"
    state.save(state_path)

    monkeypatch.setenv("PENSUM_TOKEN", "tok")
    rc = main(
        [
            "downgrade",
            "--migrations-dir",
            str(FIXTURES_DIR),
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_dc+{BASE}",
            "--auth",
            "pat",
            "-r",
            "abc123def456",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "reversed def789gh" in out
    assert "abc123def456" in out

    on_disk = StateFile.load(state_path)
    assert on_disk.revision == "abc123def456"


def test_cli_downgrade_noop_when_at_target(tmp_path, monkeypatch, capsys):
    state = StateFile(env="dev", jira_url=BASE, revision="abc123def456")
    state_path = tmp_path / "state.yaml"
    state.save(state_path)

    monkeypatch.setenv("PENSUM_TOKEN", "tok")
    rc = main(
        [
            "downgrade",
            "--migrations-dir",
            str(FIXTURES_DIR),
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_dc+{BASE}",
            "--auth",
            "pat",
            "-r",
            "abc123def456",
        ]
    )
    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out
