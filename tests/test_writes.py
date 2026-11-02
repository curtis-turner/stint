"""M6 data plane: writes via add/delete/commit + dirty tracking."""

import sys

import httpx
import pytest
import respx

import examples.adf.schema as p
from stint import (
    AsyncSession,
    PartialCommitError,
    PATAuth,
    StateFile,
    create_engine,
)
from stint.engine import Engine
from stint.exceptions import ConfigurationError
from stint.query.adf import wrap_plain_text
from stint.query.hydrate import hydrate
from stint.query.payload import (
    build_fields_payload,
    build_insert_payload,
    build_update_payload,
)
from stint.registry import registry
from stint.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    SimpleMapping,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


def _platform_state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["vuln_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100", "S2": "101", "S3": "102", "S4": "103"},
    )
    state.custom_fields["vuln_root_cause"] = CustomFieldMapping(
        id="customfield_10043",
    )
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        "customfield_10044", options={"S1": "100", "S2": "101", "S3": "102", "S4": "103"}
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
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(
        summary="boom",
        reporter="alice",
        severity="S2",
    )
    fields = build_fields_payload(bug, state, is_cloud=False)
    assert fields["customfield_10044"] == {"value": "S2"}
    assert fields["summary"] == "boom"
    assert fields["reporter"] == {"name": "alice"}


def test_payload_reporter_dc_vs_cloud():
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="acc-123", severity="S1")
    dc = build_fields_payload(bug, state, is_cloud=False)
    cloud = build_fields_payload(bug, state, is_cloud=True)
    assert dc["reporter"] == {"name": "acc-123"}
    assert cloud["reporter"] == {"accountId": "acc-123"}


def test_payload_description_wrapped_in_adf_on_cloud():
    import examples.company_managed.platform_schema as p

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
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    fields = build_fields_payload(bug, state, is_cloud=False)
    assert "assignee" not in fields
    assert "description" not in fields


def test_payload_only_filter_for_update():
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    fields = build_fields_payload(
        bug,
        state,
        is_cloud=False,
        only={"severity"},
    )
    assert set(fields) == {"customfield_10044"}


def test_payload_unmapped_custom_field_raises():
    import examples.company_managed.platform_schema as p

    state = StateFile(env="dev", jira_url=BASE)  # empty state
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    with pytest.raises(ConfigurationError) as e:
        build_fields_payload(bug, state, is_cloud=False)
    assert "bug_severity" in str(e.value)


def test_payload_attr_shadowing_declared_custom_field_raises():
    """A model attribute named like a declared CustomField but NOT Annotated
    with it would be sent as a literal key (``test_text_field``) instead of
    resolving to ``customfield_10321`` — live-tenant bug (2026-11), found via
    Jira's reply "Field 'test_text_field' cannot be set ... or unknown".
    The builder must refuse with guidance naming the actual fix."""
    import examples.company_managed.platform_schema as p
    from stint import TextField
    from stint.fields import CustomField

    _ = CustomField(alias="shadow_test", name="TestTextField", type=TextField)

    class ShadowBug(p.IssueType):
        __alias__ = "shadow_bug"
        # Deliberate mistake: attribute named like the declaration, no marker.
        test_text_field: str | None = None

    state = _platform_state()
    bug = ShadowBug(test_text_field="boom")
    with pytest.raises(ConfigurationError) as e:
        build_fields_payload(bug, state, is_cloud=False)
    assert "test_text_field" in str(e.value)
    assert "Annotated" in str(e.value)
    assert "TestTextField" in str(e.value)  # the declaration it shadows


def test_payload_attr_self_shadowing_declaration_raises_with_rename_hint():
    """The attribute *is* Annotated, but its annotation name matches the
    attribute itself. Under ``from __future__ import annotations`` the name
    resolves to the attribute's default (None), so metadata is ``[None]`` and
    the declaration is lost (live tenant, 2026-11). The error must point at
    renaming the variable, not claim the annotation is missing."""
    from typing import Annotated

    import examples.company_managed.platform_schema as p

    class SelfShadowBug(p.IssueType):
        __alias__ = "self_shadow_bug"
        # Deliberate mistake: metadata resolves to None, not the CustomField.
        test_text_field: Annotated[str | None, None] = None

    state = _platform_state()
    bug = SelfShadowBug(test_text_field="boom")
    with pytest.raises(ConfigurationError) as e:
        build_fields_payload(bug, state, is_cloud=False)
    message = str(e.value)
    assert "resolves to None" in message
    assert "Rename the" in message
    assert "TestTextField" in message


def test_insert_payload_includes_project_and_issuetype():
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1")
    body = build_insert_payload(bug, state, is_cloud=False, project_key="PLAT")
    assert body["fields"]["project"] == {"key": "PLAT"}
    assert body["fields"]["issuetype"] == {"id": "10010"}


def test_insert_payload_strips_reporter():
    """Jira Cloud sets the reporter from the caller's identity at create and
    rejects an explicit one ("Field 'reporter' cannot be set...", live-tenant
    verified) even when the field is on the create screen. Inserts therefore
    strip it — but hydration fills reporter on read, and UPDATE payloads
    still accept reporter reassignment."""
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="5b10ac8b86e24b0016d46e0f", severity="S1")
    body = build_insert_payload(bug, state, is_cloud=True, project_key="PLAT")
    assert "reporter" not in body["fields"]
    # The attribute survives on the instance (hydration path fills it on read):
    assert bug.reporter == "5b10ac8b86e24b0016d46e0f"
    # Update payloads keep it: PUT /issue accepts reporter reassignment.
    body = build_update_payload(bug, state, is_cloud=True, dirty={"reporter"})
    assert body["fields"]["reporter"] == {"accountId": "5b10ac8b86e24b0016d46e0f"}


def test_update_payload_only_dirty_fields():
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S2")
    body = build_update_payload(bug, state, is_cloud=False, dirty={"severity"})
    assert set(body["fields"]) == {"customfield_10044"}


# ── Explicit None-clearing on update ─────────────────────────────────
def test_update_payload_emits_null_for_deliberate_clear():
    """Dirty-tracked None is a deliberate clear: the update path emits null
    for optional fields rather than silently dropping them (B4)."""
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    bug = p.Bug(summary="x", reporter="alice", severity="S1", assignee="acc-1")
    bug.assignee = None
    body = build_update_payload(bug, state, is_cloud=False, dirty={"assignee"})
    assert body["fields"] == {"assignee": None}


def test_update_payload_null_for_optional_custom_field_clear():
    import examples.company_managed.platform_schema as p

    state = _platform_state()
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(
        id="customfield_10045",
    )
    bug = p.Bug(summary="x", reporter="alice", severity="S1", root_cause="cache")
    bug.root_cause = None
    body = build_update_payload(bug, state, is_cloud=True, dirty={"root_cause"})
    assert body["fields"] == {"customfield_10045": None}


# ── Project inference / __projects__ linkage ─────────────────────────
def test_project_meta_sets_projects_on_issuetype():
    import examples.company_managed.platform_schema as p

    assert "PLAT" in p.Bug.__projects__


# ── add / delete plumbing (no commit) ────────────────────────────────
def test_add_rejects_instance_with_existing_key():
    import examples.company_managed.platform_schema as p

    engine = _cloud_engine()
    session = AsyncSession(engine, _platform_state())
    bug = p.Bug(key="PLAT-1", summary="x", reporter="a", severity="S1")
    with pytest.raises(ConfigurationError) as e:
        session.add(bug)
    assert "already has key" in str(e.value)


def test_add_with_explicit_project_overrides_inferred():
    import examples.company_managed.platform_schema as p

    engine = _cloud_engine()
    session = AsyncSession(engine, _platform_state())
    bug = p.Bug(summary="x", reporter="a", severity="S1")
    session.add(bug, project="PLAT")
    assert len(session._pending_inserts) == 1
    assert session._pending_inserts[0][1] == "PLAT"


def test_delete_requires_key():
    import examples.company_managed.platform_schema as p

    engine = _cloud_engine()
    session = AsyncSession(engine, _platform_state())
    bug = p.Bug(summary="x", reporter="a", severity="S1")
    with pytest.raises(ConfigurationError):
        session.delete(bug)


# ── End-to-end: insert ──────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_commit_insert_posts_issue_and_sets_key():
    import examples.company_managed.platform_schema as p

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
    import examples.company_managed.platform_schema as p

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
    # reporter is stripped from inserts (Jira sets it from the caller identity):
    assert "reporter" not in captured[0]["fields"]


# ── End-to-end: update via dirty tracking ────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_dirty_tracking_emits_minimal_put():
    """Hydrate via get(), mutate one field, commit → PUT with only that field."""
    import json

    import examples.company_managed.platform_schema as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "old",
                    "reporter": {"name": "alice"},
                    "customfield_10044": {"value": "S1"},
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
            assert bug is not None
            bug.severity = "S3"
            await session.commit()
        finally:
            await engine.close()

    # Only the dirty field appears in the PUT body.
    assert captured_body[0]["fields"] == {"customfield_10044": {"value": "S3"}}


@pytest.mark.asyncio
@respx.mock
async def test_dirty_none_assignment_puts_null_to_clear():
    """Setting an optional field to None on a hydrated instance must PUT a
    null once (clearing the field server-side), not skip the field and
    silently report success while losing the change (B4)."""
    import json

    import examples.company_managed.platform_schema as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "x",
                    "reporter": {"accountId": "acc-1"},
                    "assignee": {"accountId": "acc-2"},
                    "customfield_10044": {"value": "S1"},
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
            assert bug is not None
            bug.assignee = None
            await session.commit()
        finally:
            await engine.close()

    assert captured_body[0]["fields"] == {"assignee": None}


@pytest.mark.asyncio
@respx.mock
async def test_no_dirty_changes_means_no_put():
    import examples.company_managed.platform_schema as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "x",
                    "reporter": {"name": "alice"},
                    "customfield_10044": {"value": "S1"},
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
    import examples.company_managed.platform_schema as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "x",
                    "bug_severity": {"value": "S1"},
                    "reporter": {"name": "alice"},
                    "customfield_10044": {"value": "S1"},
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
            assert bug is not None
            session.delete(bug)
            results = await session.commit()
        finally:
            await engine.close()
    assert results[-1].operation == "delete"
    assert results[-1].success
    assert (p.Bug, "PLAT-1") not in session._identity


def test_hydrate_description_from_adf():
    # ADF payload for description
    issue = {
        "key": "PLAT-1",
        "fields": {
            "summary": "x",
            "reporter": {"name": "alice"},
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "hello world"},
                        ],
                    }
                ],
            },
        },
    }
    bug = hydrate(p.Foo, issue, _platform_state())
    assert bug.description == "hello world"


# ── Mixed success/failure raises PartialCommitError ──────────────────
@pytest.mark.asyncio
@respx.mock
async def test_partial_commit_error_on_mixed_results():
    """First insert succeeds, second fails 400. Exception carries both results."""
    import examples.company_managed.platform_schema as p

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


def test_partial_commit_error_with_no_failures_does_not_crash():
    """Constructing the exception with an all-success (or empty) result list
    must not raise IndexError from the message formatting — the IndexError
    would mask the caller's actual error (L5)."""
    from stint.exceptions import PartialCommitError

    err = PartialCommitError(results=[])
    assert not err.failures
    assert not err.successes
    assert "0 failed" in str(err)


# ── Commit clears pending queues ─────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_commit_clears_pending_inserts_and_deletes():
    import examples.company_managed.platform_schema as p

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
