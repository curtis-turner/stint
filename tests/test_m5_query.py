"""M5 data plane (reads): expression AST, JQL compile, session, identity map."""

import sys

import httpx
import pytest
import respx

from pensum import (
    AsyncSession,
    PATAuth,
    StateFile,
    create_engine,
    not_,
    or_,
    select,
)
from pensum.engine import Engine
from pensum.registry import registry
from pensum.state.file import CustomFieldMapping

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
    """A state file matching the platform example's schema."""
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100", "S2": "101", "S3": "102", "S4": "103"},
    )
    state.custom_fields["bug_root_cause"] = CustomFieldMapping(
        id="customfield_10043",
    )
    return state


# ── Column access ────────────────────────────────────────────────────
def test_bug_c_severity_returns_column():
    import examples.platform as p

    col = p.Bug.c.severity
    assert col.attr_name == "severity"
    assert col.cf_alias == "bug_severity"


def test_bug_c_summary_is_system_field():
    import examples.platform as p

    col = p.Bug.c.summary
    assert col.attr_name == "summary"
    assert col.cf_alias is None


def test_bug_c_unknown_attr_raises():
    import examples.platform as p

    with pytest.raises(AttributeError) as e:
        _ = p.Bug.c.does_not_exist
    assert "Known fields" in str(e.value)


# ── JQL compile ──────────────────────────────────────────────────────
def test_compile_eq_on_custom_field_resolves_to_cf_notation():
    import examples.platform as p

    stmt = select(p.Bug).where(p.Bug.c.severity == "S1")
    jql = stmt.compile(_platform_state())
    assert jql == 'cf[10042] = "S1"'


def test_compile_eq_on_system_field_uses_attr_name():
    import examples.platform as p

    stmt = select(p.Bug).where(p.Bug.c.summary == "boom")
    assert stmt.compile(_platform_state()) == 'summary = "boom"'


def test_compile_multiple_filters_and_joined():
    import examples.platform as p

    stmt = select(p.Bug).where(
        p.Bug.c.severity == "S1",
        p.Bug.c.reporter == "jdoe",
    )
    jql = stmt.compile(_platform_state())
    assert jql == '(cf[10042] = "S1" AND reporter = "jdoe")'


def test_compile_or_and_not():
    import examples.platform as p

    expr = or_(
        p.Bug.c.severity == "S1",
        p.Bug.c.severity == "S2",
    )
    stmt = select(p.Bug).where(not_(expr))
    jql = stmt.compile(_platform_state())
    assert jql == 'NOT ((cf[10042] = "S1" OR cf[10042] = "S2"))'


def test_compile_in_operator():
    import examples.platform as p

    stmt = select(p.Bug).where(p.Bug.c.severity.in_(["S1", "S2"]))
    jql = stmt.compile(_platform_state())
    assert jql == 'cf[10042] in ("S1", "S2")'


def test_compile_contains_substring():
    import examples.platform as p

    stmt = select(p.Bug).where(p.Bug.c.summary.contains("login"))
    assert stmt.compile(_platform_state()) == 'summary ~ "login"'


def test_compile_is_null():
    import examples.platform as p

    stmt = select(p.Bug).where(p.Bug.c.assignee.is_null())
    assert stmt.compile(_platform_state()) == "assignee is EMPTY"


def test_compile_order_by():
    import examples.platform as p

    stmt = (
        select(p.Bug)
        .where(p.Bug.c.severity == "S1")
        .order_by(
            p.Bug.c.created,
            "DESC",
        )
    )
    jql = stmt.compile(_platform_state())
    assert jql == 'cf[10042] = "S1" ORDER BY created DESC'


def test_compile_unmapped_custom_field_raises():
    """If state lacks the alias, compile errors with an actionable message."""
    import examples.platform as p

    state = StateFile(env="dev", jira_url=BASE)  # empty state
    stmt = select(p.Bug).where(p.Bug.c.severity == "S1")
    with pytest.raises(KeyError) as e:
        stmt.compile(state)
    assert "bug_severity" in str(e.value)
    assert "pensum stamp" in str(e.value)


# ── End-to-end ────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_session_get_by_key_hits_issue_endpoint():
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

    state = _platform_state()
    engine = _cloud_engine()
    async with AsyncSession(engine, state) as session:
        try:
            bug = await session.get(p.Bug, "PLAT-7")
        finally:
            await engine.close()

    assert bug is not None
    assert bug.key == "PLAT-7"
    assert bug.severity == "S3"


@pytest.mark.asyncio
@respx.mock
async def test_session_get_returns_none_on_404():
    import examples.platform as p

    respx.get(f"{CLOUD_ROOT}/issue/MISSING-1").mock(return_value=httpx.Response(404))
    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            assert await session.get(p.Bug, "MISSING-1") is None
        finally:
            await engine.close()


# ── Identity map ─────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_identity_map_returns_same_instance():
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

    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            first = await session.get(p.Bug, "PLAT-1")
            second = await session.get(p.Bug, "PLAT-1")
        finally:
            await engine.close()
    assert first is second
    # And only ONE HTTP call (respx will record one match).
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_scalars_then_get_reuses_identity():
    """Issue returned by scalars() is reused by subsequent .get()."""
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
    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            results = await session.scalars(
                select(p.Bug).where(p.Bug.c.severity == "S1"),
            )
            cached = await session.get(p.Bug, "PLAT-1")
        finally:
            await engine.close()
    assert results[0] is cached
    # Only the /search/jql call, no /issue/ call
    assert respx.calls.call_count == 1


# ── Cloud path ───────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_session_scalars_cloud_uses_search_jql():
    import examples.platform as p

    respx.post(f"{CLOUD_ROOT}/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "PLAT-1",
                        "fields": {
                            "summary": "cloud bug",
                            "reporter": {"accountId": "acc-1"},
                            "customfield_10042": {"value": "S1"},
                        },
                    },
                ],
            },
        )
    )

    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            results = await session.scalars(
                select(p.Bug).where(p.Bug.c.severity == "S1"),
            )
        finally:
            await engine.close()

    assert results[0].reporter == "acc-1"  # Cloud → accountId


@pytest.mark.asyncio
@respx.mock
async def test_cloud_pagination_via_next_page_token():
    import examples.platform as p

    # First page returns nextPageToken, second page returns none.
    respx.post(f"{CLOUD_ROOT}/search/jql").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "issues": [
                        {
                            "key": "PLAT-1",
                            "fields": {
                                "summary": "p1",
                                "reporter": {"accountId": "a"},
                                "customfield_10042": {"value": "S1"},
                            },
                        }
                    ],
                    "nextPageToken": "tok-2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "issues": [
                        {
                            "key": "PLAT-2",
                            "fields": {
                                "summary": "p2",
                                "reporter": {"accountId": "b"},
                                "customfield_10042": {"value": "S2"},
                            },
                        }
                    ],
                },
            ),
        ]
    )

    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            results = await session.scalars(select(p.Bug))
        finally:
            await engine.close()
    assert [r.key for r in results] == ["PLAT-1", "PLAT-2"]


# ── limit truncates results before the next page is fetched ──────────
@pytest.mark.asyncio
@respx.mock
async def test_limit_truncates_results():
    import examples.platform as p

    # Page one advertises a nextPageToken, but limit(1) stops the generator
    # before the second page is ever requested.
    respx.post(f"{CLOUD_ROOT}/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "PLAT-1",
                        "fields": {
                            "summary": "p1",
                            "reporter": {"accountId": "a"},
                            "customfield_10042": {"value": "S1"},
                        },
                    }
                ],
                "nextPageToken": "tok-2",
            },
        )
    )

    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            results = await session.scalars(select(p.Bug).limit(1))
        finally:
            await engine.close()
    assert [r.key for r in results] == ["PLAT-1"]


# ── No filters → empty JQL → all issues ──────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_select_no_filters_emits_empty_jql():
    import json

    import examples.platform as p

    captured: list[str] = []

    def _capture(request):
        captured.append(json.loads(request.content).get("jql", ""))
        return httpx.Response(200, json={"issues": []})

    respx.post(f"{CLOUD_ROOT}/search/jql").mock(side_effect=_capture)
    engine = _cloud_engine()
    async with AsyncSession(engine, _platform_state()) as session:
        try:
            await session.scalars(select(p.Bug))
        finally:
            await engine.close()
    assert captured == [""]
