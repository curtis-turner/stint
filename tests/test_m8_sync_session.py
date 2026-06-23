"""M8 sync Session: facade over AsyncSession with a dedicated event loop.

Mirrors a slice of the M5/M6 coverage to confirm the wrapper preserves
read, write, and identity-map semantics. Plus the lifecycle guarantees
(closes the loop, refuses to construct inside a running loop, exits cleanly).
"""

from __future__ import annotations

import asyncio
import sys

import httpx
import pytest
import respx

from stint import (
    PartialCommitError,
    PATAuth,
    Session,
    StateFile,
    create_engine,
    select,
)
from stint.engine import Engine
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
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(id="customfield_10043")
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


# ── Lifecycle ────────────────────────────────────────────────────────
def test_session_refuses_inside_running_loop():
    """Constructing Session under an active loop must raise — the wrapper
    can't drive its private loop from inside another one."""

    async def _inner():
        with pytest.raises(RuntimeError, match="running event loop"):
            Session(_cloud_engine(), _platform_state())

    asyncio.run(_inner())


def test_session_context_manager_closes_loop():
    engine = _cloud_engine()
    with Session(engine, _platform_state()) as session:
        assert not session._closed
    assert session._closed
    assert session._loop.is_closed()


def test_session_close_is_idempotent():
    engine = _cloud_engine()
    session = Session(engine, _platform_state())
    session.close()
    session.close()  # must not raise
    assert session._loop.is_closed()


def test_session_close_engine_false_keeps_engine_open():
    engine = _cloud_engine()
    session = Session(engine, _platform_state(), close_engine=False)
    session.close()
    # Engine's underlying http client should still be usable; close it now.
    assert not engine.client._client.is_closed
    asyncio.run(engine.close())


# ── Reads ────────────────────────────────────────────────────────────
@respx.mock
def test_session_get_returns_hydrated_instance():
    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-7").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-7",
                "fields": {
                    "summary": "specific bug",
                    "reporter": {"name": "alice"},
                    "customfield_10042": {"value": "S3"},
                },
            },
        )
    )
    with Session(_cloud_engine(), _platform_state()) as session:
        bug = session.get(p.Bug, "PLAT-7")

    assert bug is not None
    assert bug.key == "PLAT-7"
    assert bug.severity == "S3"


@respx.mock
def test_session_get_returns_none_on_404():
    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/MISSING-1").mock(return_value=httpx.Response(404))
    with Session(_cloud_engine(), _platform_state()) as session:
        assert session.get(p.Bug, "MISSING-1") is None


@respx.mock
def test_session_scalars_round_trip():
    import examples.platform as p

    respx.post(f"{CLOUD_ROOT}/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "PLAT-1",
                        "fields": {
                            "summary": "s",
                            "reporter": {"accountId": "acc-1"},
                            "customfield_10042": {"value": "S1"},
                        },
                    }
                ],
            },
        )
    )
    with Session(_cloud_engine(), _platform_state()) as session:
        results = session.scalars(select(p.Bug).where(p.Bug.c.severity == "S1"))

    assert len(results) == 1
    assert results[0].key == "PLAT-1"
    assert results[0].severity == "S1"


@respx.mock
def test_session_identity_map_preserved():
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
    with Session(_cloud_engine(), _platform_state()) as session:
        first = session.get(p.Bug, "PLAT-1")
        second = session.get(p.Bug, "PLAT-1")

    assert first is second
    assert respx.calls.call_count == 1


# ── Writes ───────────────────────────────────────────────────────────
@respx.mock
def test_session_add_and_commit_inserts_issue():
    import examples.platform as p

    respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"id": "10000", "key": "PLAT-100"}))
    bug = p.Bug(summary="boom", reporter="alice", severity="S2")
    with Session(_cloud_engine(), _platform_state()) as session:
        session.add(bug)
        results = session.commit()

    assert bug.key == "PLAT-100"
    assert len(results) == 1
    assert results[0].success
    assert results[0].operation == "insert"


@respx.mock
def test_session_dirty_update_commits_only_changed_fields():
    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "summary": "original",
                    "reporter": {"name": "alice"},
                    "customfield_10042": {"value": "S3"},
                },
            },
        )
    )
    put_route = respx.put(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(204))

    with Session(_cloud_engine(), _platform_state()) as session:
        bug = session.get(p.Bug, "PLAT-1")
        bug.severity = "S1"
        session.commit()

    assert put_route.called
    body = put_route.calls.last.request.content
    # Only the severity custom field should appear in the PUT body.
    assert b"customfield_10042" in body
    assert b"summary" not in body


@respx.mock
def test_session_partial_commit_raises():
    import examples.platform as p

    # First insert succeeds, second fails with 400.
    respx.post(f"{CLOUD_ROOT}/issue").mock(
        side_effect=[
            httpx.Response(201, json={"id": "1", "key": "PLAT-1"}),
            httpx.Response(400, json={"errors": {"summary": "required"}}),
        ]
    )

    ok = p.Bug(summary="good", reporter="alice", severity="S1")
    bad = p.Bug(summary="also good", reporter="alice", severity="S2")
    with Session(_cloud_engine(), _platform_state()) as session:
        session.add(ok)
        session.add(bad)
        with pytest.raises(PartialCommitError) as exc:
            session.commit()

    err = exc.value
    assert len(err.successes) == 1
    assert len(err.failures) == 1
    assert ok.key == "PLAT-1"  # first insert did commit
