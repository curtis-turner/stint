"""M6 data plane: writes via add/delete/commit + dirty tracking."""

import sys

import httpx
import pytest
import respx

from pensum import (
    AsyncSession,
    PartialCommitError,
    PATAuth,
    StateFile,
    create_engine,
)
from pensum.engine import Engine
from pensum.exceptions import ConfigurationError
from pensum.query.adf import wrap_plain_text
from pensum.query.payload import (
    build_fields_payload,
    build_insert_payload,
    build_update_payload,
)
from pensum.registry import registry
from pensum.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    SimpleMapping,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.platform", None)


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


def _platform_state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100", "S2": "101", "S3": "102", "S4": "103"},
    )
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(
        id="customfield_10043",
    )
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


# ── ADF helper ───────────────────────────────────────────────────────
def test_wrap_plain_text_minimal():
    doc = wrap_plain_text("hello world")
    assert doc == {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]},
        ],
    }


def test_wrap_plain_text_multi_paragraph():
    doc = wrap_plain_text("first.\n\nsecond.")
    assert len(doc["content"]) == 2
    assert doc["content"][0]["content"][0]["text"] == "first."
    assert doc["content"][1]["content"][0]["text"] == "second."


def test_wrap_plain_text_empty():
    assert wrap_plain_text("") == {"type": "doc", "version": 1, "content": []}


# ── Payload construction ─────────────────────────────────────────────
def test_payload_select_field_emitted_as_value_object():
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(
        summary="boom",
        reporter="alice",
        severity="S2",
    )
    fields = build_fields_payload(bug, state, is_cloud=False)
    assert fields["customfield_10042"] == {"value": "S2"}
    assert fields["summary"] == "boom"
    assert fields["reporter"] == {"name": "alice"}


def test_payload_reporter_dc_vs_cloud():
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="acc-123", severity="S1")
    dc = build_fields_payload(bug, state, is_cloud=False)
    cloud = build_fields_payload(bug, state, is_cloud=True)
    assert dc["reporter"] == {"name": "acc-123"}
    assert cloud["reporter"] == {"accountId": "acc-123"}


def test_payload_description_wrapped_in_adf_on_cloud():
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(
        summary="x",
        reporter="alice",
        severity="S1",
        description="multi-line\n\nbody",
    )
    dc = build_fields_payload(bug, state, is_cloud=False)
    cloud = build_fields_payload(bug, state, is_cloud=True)
    assert dc["description"] == "multi-line\n\nbody"  # raw string
    assert cloud["description"]["type"] == "doc"
    assert len(cloud["description"]["content"]) == 2  # two paragraphs


def test_payload_omits_unset_optionals():
    """assignee is Optional[str] = None; description default None. Skipped
    so a PUT doesn't accidentally clear existing Jira data."""
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    fields = build_fields_payload(bug, state, is_cloud=False)
    assert "assignee" not in fields
    assert "description" not in fields


def test_payload_only_filter_for_update():
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    fields = build_fields_payload(
        bug,
        state,
        is_cloud=False,
        only={"severity"},
    )
    assert set(fields) == {"customfield_10042"}


def test_payload_unmapped_custom_field_raises():
    import examples.platform as p

    state = StateFile(env="dev", jira_url=BASE)  # empty state
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    with pytest.raises(ConfigurationError) as e:
        build_fields_payload(bug, state, is_cloud=False)
    assert "bug_severity" in str(e.value)


def test_insert_payload_includes_project_and_issuetype():
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    body = build_insert_payload(bug, state, is_cloud=False, project_key="PLAT")
    assert body["fields"]["project"] == {"id": "proj-1"}
    assert body["fields"]["issuetype"] == {"id": "10010"}


def test_update_payload_only_dirty_fields():
    import examples.platform as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S2")
    body = build_update_payload(bug, state, is_cloud=False, dirty={"severity"})
    assert set(body["fields"]) == {"customfield_10042"}


# ── Project inference / __projects__ linkage ─────────────────────────
def test_project_meta_sets_projects_on_issuetype():
    import examples.platform as p

    assert "PLAT" in p.Bug.__projects__


# ── add / delete plumbing (no commit) ────────────────────────────────
def test_add_rejects_instance_with_existing_key():
    import examples.platform as p

    engine = _cloud_engine()
    session = AsyncSession(engine, _platform_state())
    bug = p.Bug(key="PLAT-1", summary="x", reporter="a", severity="S1")
    with pytest.raises(ConfigurationError) as e:
        session.add(bug)
    assert "already has key" in str(e.value)


def test_add_with_explicit_project_overrides_inferred():
    import examples.platform as p

    engine = _cloud_engine()
    session = AsyncSession(engine, _platform_state())
    bug = p.Bug(summary="x", reporter="a", severity="S1")
    session.add(bug, project="PLAT")
    assert len(session._pending_inserts) == 1
    assert session._pending_inserts[0][1] == "PLAT"


def test_delete_requires_key():
    import examples.platform as p

    engine = _cloud_engine()
    session = AsyncSession(engine, _platform_state())
    bug = p.Bug(summary="x", reporter="a", severity="S1")
    with pytest.raises(ConfigurationError):
        session.delete(bug)


# ── End-to-end: insert ──────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_commit_insert_posts_issue_and_sets_key():
    import examples.platform as p

    respx.post(f"{CLOUD_ROOT}/issue").mock(
        return_value=httpx.Response(
            201,
            json={"id": "issue-1", "key": "PLAT-7"},
        )
    )

    engine = _cloud_engine()
    state = _platform_state()
    bug = p.Bug(summary="boom", reporter="alice", severity="S1")
    async with AsyncSession(engine, state) as session:
        try:
            session.add(bug)
            results = await session.commit()
        finally:
            await engine.close()

    assert results[0].operation == "insert"
    assert results[0].success
    assert bug.key == "PLAT-7"  # mutated in place
    # The instance is now identity-cached:
    assert session._identity[(p.Bug, "PLAT-7")] is bug


@pytest.mark.asyncio
@respx.mock
async def test_commit_insert_cloud_uses_adf_description():
    import examples.platform as p

    captured: list[dict] = []

    def _record(request):
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "i", "key": "PLAT-1"})

    respx.post(f"{CLOUD_ROOT}/issue").mock(side_effect=_record)

    engine = _cloud_engine()
    state = _platform_state()
    bug = p.Bug(
        summary="x",
        reporter="acc-1",
        severity="S1",
        description="hello world",
    )
    async with AsyncSession(engine, state) as session:
        try:
            session.add(bug)
            await session.commit()
        finally:
            await engine.close()

    assert captured[0]["fields"]["description"]["type"] == "doc"
    assert captured[0]["fields"]["reporter"] == {"accountId": "acc-1"}


# ── End-to-end: update via dirty tracking ────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_dirty_tracking_emits_minimal_put():
    """Hydrate via get(), mutate one field, commit → PUT with only that field."""
    import json

    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "old",
                    "reporter": {"name": "alice"},
                    "customfield_10042": {"value": "S1"},
                },
            },
        )
    )

    captured_body: list[dict] = []

    def _record(request):
        captured_body.append(json.loads(request.content))
        return httpx.Response(204)

    respx.put(f"{CLOUD_ROOT}/issue/PLAT-1").mock(side_effect=_record)

    engine = _cloud_engine()
    state = _platform_state()
    async with AsyncSession(engine, state) as session:
        try:
            bug = await session.get(p.Bug, "PLAT-1")
            bug.severity = "S3"
            await session.commit()
        finally:
            await engine.close()

    # Only the dirty field appears in the PUT body.
    assert captured_body[0]["fields"] == {"customfield_10042": {"value": "S3"}}


@pytest.mark.asyncio
@respx.mock
async def test_no_dirty_changes_means_no_put():
    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "x",
                    "reporter": {"name": "alice"},
                    "customfield_10042": {"value": "S1"},
                },
            },
        )
    )
    # No PUT mock — if commit fires one, respx raises.

    engine = _cloud_engine()
    state = _platform_state()
    async with AsyncSession(engine, state) as session:
        try:
            await session.get(p.Bug, "PLAT-1")  # hydrate, no mutation
            results = await session.commit()
        finally:
            await engine.close()
    # No update operation recorded (only the GET happened):
    assert results == []


# ── End-to-end: delete ───────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_commit_delete_calls_dialect():
    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "x",
                    "reporter": {"name": "alice"},
                    "customfield_10042": {"value": "S1"},
                },
            },
        )
    )
    respx.delete(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(204))

    engine = _cloud_engine()
    state = _platform_state()
    async with AsyncSession(engine, state) as session:
        try:
            bug = await session.get(p.Bug, "PLAT-1")
            session.delete(bug)
            results = await session.commit()
        finally:
            await engine.close()
    assert results[-1].operation == "delete"
    assert results[-1].success
    assert (p.Bug, "PLAT-1") not in session._identity


# ── Mixed success/failure raises PartialCommitError ──────────────────
@pytest.mark.asyncio
@respx.mock
async def test_partial_commit_error_on_mixed_results():
    """First insert succeeds, second fails 400. Exception carries both results."""
    import examples.platform as p

    respx.post(f"{CLOUD_ROOT}/issue").mock(
        side_effect=[
            httpx.Response(201, json={"id": "i1", "key": "PLAT-1"}),
            httpx.Response(400, json={"errorMessages": ["bad input"]}),
        ]
    )

    engine = _cloud_engine()
    state = _platform_state()
    async with AsyncSession(engine, state) as session:
        try:
            session.add(p.Bug(summary="ok", reporter="a", severity="S1"))
            session.add(p.Bug(summary="bad", reporter="a", severity="S1"))
            with pytest.raises(PartialCommitError) as e:
                await session.commit()
        finally:
            await engine.close()

    assert len(e.value.successes) == 1
    assert len(e.value.failures) == 1
    # First successful insert kept its key:
    assert e.value.successes[0].instance.key == "PLAT-1"
    # Second instance never got a key:
    assert e.value.failures[0].instance.key is None
    assert "bad input" in str(e.value.failures[0].error) or "400" in str(e.value.failures[0].error)


# ── Commit clears pending queues ─────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_commit_clears_pending_inserts_and_deletes():
    import examples.platform as p

    respx.post(f"{CLOUD_ROOT}/issue").mock(
        return_value=httpx.Response(
            201,
            json={"id": "i", "key": "PLAT-1"},
        )
    )

    engine = _cloud_engine()
    state = _platform_state()
    async with AsyncSession(engine, state) as session:
        try:
            session.add(p.Bug(summary="x", reporter="a", severity="S1"))
            await session.commit()
        finally:
            await engine.close()
    assert session._pending_inserts == []
    assert session._pending_deletes == []
