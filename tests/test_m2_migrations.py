"""Migration system: loader, graph, runner, op API. End-to-end via respx."""

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
from pensum.migrations.exceptions import (
    MigrationConflictError,
    MigrationGraphError,
    UnsupportedDowngradeError,
)
from pensum.migrations.runner import upgrade as run_upgrade

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "migrations"


# ── Loader / graph ────────────────────────────────────────────────────
def test_loader_picks_up_migration_files():
    graph = load_migrations(FIXTURES_DIR)
    assert set(graph.by_revision) == {"abc123def456", "def789ghi012"}


def test_loader_resolves_chain_order():
    graph = load_migrations(FIXTURES_DIR)
    chain = graph.chain_from(None)
    assert [m.revision for m in chain] == ["abc123def456", "def789ghi012"]


def test_loader_chain_from_intermediate():
    graph = load_migrations(FIXTURES_DIR)
    chain = graph.chain_from("abc123def456")
    assert [m.revision for m in chain] == ["def789ghi012"]


def test_loader_chain_from_head_is_empty():
    graph = load_migrations(FIXTURES_DIR)
    assert graph.chain_from("def789ghi012") == []


def test_loader_unknown_current_raises(tmp_path):
    graph = load_migrations(FIXTURES_DIR)
    with pytest.raises(MigrationGraphError) as e:
        graph.chain_from("nonexistent_rev")
    assert "not in the migration graph" in str(e.value)


def test_loader_duplicate_revision_raises(tmp_path):
    """Two files with the same `revision` global cannot coexist."""
    (tmp_path / "a.py").write_text(
        "revision = 'dup'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "b.py").write_text(
        "revision = 'dup'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    with pytest.raises(MigrationConflictError):
        load_migrations(tmp_path)


def test_loader_orphan_down_revision_raises(tmp_path):
    (tmp_path / "a.py").write_text(
        "revision = 'orphan'\n"
        "down_revision = 'missing_parent'\n"
        "async def upgrade(): pass\n"
        "async def downgrade(): pass\n"
    )
    with pytest.raises(MigrationGraphError):
        load_migrations(tmp_path)


def test_multiple_heads_blocks_chain(tmp_path):
    """Two migrations off the same parent must be merged explicitly."""
    (tmp_path / "a.py").write_text(
        "revision = 'base'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "b.py").write_text(
        "revision = 'left'\ndown_revision = 'base'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "c.py").write_text(
        "revision = 'right'\ndown_revision = 'base'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    graph = load_migrations(tmp_path)
    with pytest.raises(MigrationGraphError) as e:
        graph.chain_from(None)
    assert "multiple heads" in str(e.value).lower()


# ── End-to-end upgrade against respx ──────────────────────────────────
def _engine():
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


@pytest.mark.asyncio
@respx.mock
async def test_upgrade_runs_initial_migration_and_records_revision(tmp_path):
    """The first migration creates a select field with 4 options. End-to-end:
    - POST /field is called once
    - POST /field/{id}/option is called once per option (4 times)
    - State file records the new alias mapping and the revision marker
    """
    respx.post(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(201, json={"id": "customfield_10042", "name": "Severity"})
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"id": "ctx-1"}], "isLast": True, "startAt": 0, "maxResults": 1},
        )
    )
    for opt, opt_id in [("S1", "100"), ("S2", "101"), ("S3", "102"), ("S4", "103")]:
        respx.post(
            f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
            json__eq={"options": [{"value": opt}]},
        ).mock(return_value=httpx.Response(200, json={"options": [{"id": opt_id, "value": opt}]}))

    state = StateFile(env="dev", jira_url=BASE)
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)

    engine = _engine()
    try:
        applied = await run_upgrade(engine, state, graph, state_path)
    finally:
        await engine.close()

    assert [m.revision for m in applied] == ["abc123def456", "def789ghi012"]
    assert state.revision == "def789ghi012"
    assert state.custom_fields["bug_severity"].id == "customfield_10042"
    assert state.custom_fields["bug_severity"].options == {
        "S1": "100",
        "S2": "101",
        "S3": "102",
        "S4": "103",
    }


@pytest.mark.asyncio
@respx.mock
async def test_second_migration_only_runs_from_intermediate(tmp_path):
    """If state.revision is already at the first revision, only the second runs."""
    respx.post(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(201, json={"id": "customfield_10043", "name": "Root Cause"})
    )

    state = StateFile(env="dev", jira_url=BASE, revision="abc123def456")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)

    engine = _engine()
    try:
        applied = await run_upgrade(engine, state, graph, state_path)
    finally:
        await engine.close()

    assert [m.revision for m in applied] == ["def789ghi012"]
    assert state.revision == "def789ghi012"
    assert state.custom_fields["bug_root_cause"].id == "customfield_10043"


@pytest.mark.asyncio
async def test_upgrade_at_head_is_noop(tmp_path):
    state = StateFile(env="dev", jira_url=BASE, revision="def789ghi012")
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)
    engine = _engine()
    try:
        applied = await run_upgrade(engine, state, graph, state_path)
    finally:
        await engine.close()
    assert applied == []
    assert state.revision == "def789ghi012"


@pytest.mark.asyncio
@respx.mock
async def test_state_file_persisted_after_each_migration(tmp_path):
    """Even if a later migration would fail, the state at the last good revision
    is on disk."""
    respx.post(f"{CLOUD_ROOT}/field").mock(
        side_effect=[
            # first migration's POST succeeds
            httpx.Response(201, json={"id": "customfield_10042", "name": "Severity"}),
            # second migration's POST fails with 500
            httpx.Response(500, json={"errorMessages": ["server boom"]}),
        ]
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"id": "ctx-1"}], "isLast": True, "startAt": 0, "maxResults": 1},
        )
    )
    for opt, opt_id in [("S1", "100"), ("S2", "101"), ("S3", "102"), ("S4", "103")]:
        respx.post(
            f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
            json__eq={"options": [{"value": opt}]},
        ).mock(return_value=httpx.Response(200, json={"options": [{"id": opt_id, "value": opt}]}))

    state = StateFile(env="dev", jira_url=BASE)
    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)
    engine = _engine()
    try:
        from pensum.exceptions import TransportError

        with pytest.raises(TransportError):
            await run_upgrade(engine, state, graph, state_path)
    finally:
        await engine.close()

    on_disk = StateFile.load(state_path)
    assert on_disk.revision == "abc123def456"
    assert on_disk.custom_fields["bug_severity"].id == "customfield_10042"
    assert "bug_root_cause" not in on_disk.custom_fields


# ── op.unsupported / downgrade guard ──────────────────────────────────
@pytest.mark.asyncio
async def test_unsupported_downgrade_raises(tmp_path):
    """The first migration's downgrade calls op.unsupported(...); runner aborts."""
    from pensum.migrations.context import MigrationContext, reset_context, set_context

    state = StateFile(env="dev", jira_url=BASE, revision="abc123def456")
    engine = _engine()
    try:
        graph = load_migrations(FIXTURES_DIR)
        migration = graph.by_revision["abc123def456"]
        ctx = MigrationContext(engine=engine, state=state, direction="downgrade")
        token = set_context(ctx)
        try:
            with pytest.raises(UnsupportedDowngradeError):
                await migration.downgrade()
        finally:
            reset_context(token)
    finally:
        await engine.close()


# ── CustomField name collision in state ───────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_custom_field_is_idempotent_when_alias_in_state(tmp_path):
    """M4 idempotency: when alias is already in state, the op is a no-op
    (returns existing id, no HTTP). This makes re-running a partially-applied
    migration safe — the previously-completed ops simply pass through."""
    from pensum.state.file import CustomFieldMapping

    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100", "S2": "101", "S3": "102", "S4": "103"},
    )
    # The second migration creates bug_root_cause, which we DO need to allow.
    respx.post(f"{CLOUD_ROOT}/field").mock(return_value=httpx.Response(201, json={"id": "customfield_10043"}))

    state_path = tmp_path / "state.yaml"
    graph = load_migrations(FIXTURES_DIR)
    engine = _engine()
    try:
        applied = await run_upgrade(engine, state, graph, state_path)
    finally:
        await engine.close()
    assert [m.revision for m in applied] == ["abc123def456", "def789ghi012"]
    # bug_severity unchanged (was pre-existing in state); no POST /field for it.
    assert state.custom_fields["bug_severity"].id == "customfield_10042"
    assert state.custom_fields["bug_root_cause"].id == "customfield_10043"
