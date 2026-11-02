"""M2 expanded op surface: every op in stint.op, end-to-end via respx.

Each test sets up the smallest state preconditions, runs the op inside a
fresh MigrationContext, and asserts on Jira HTTP calls plus state mutations.
The runner is exercised by test_m2_migrations.py; tests here focus on the
op-level alias resolution and dialect contract.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from stint import PATAuth, StateFile, create_engine, op
from stint.engine import Engine
from stint.exceptions import ConfigurationError
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    ScreenMapping,
    SimpleMapping,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


def _sent_json(response_call: Any) -> Any:
    """Parse the JSON body a recorded request sent.

    httpx(the lowest-declared floor version) serializes `json=` payloads
    with space separators while newer releases use compact form, so asserts
    on what stint put *in* the request must parse it, never match bytes.
    """
    return json.loads(response_call.request.read())


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


async def _run_in_ctx(
    engine: Engine,
    state: StateFile,
    body: Callable[[], Awaitable[object]],
) -> object:
    ctx = MigrationContext(engine=engine, state=state, direction="upgrade")
    token = set_context(ctx)
    try:
        return await body()
    finally:
        reset_context(token)


# ── Screens ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_screen_records_id():
    respx.post(
        f"{CLOUD_ROOT}/screens",
        json__eq={
            "name": "Bug Screen",
            "description": "for bugs",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "scr-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_screen(
                alias="bug_screen",
                name="Bug Screen",
                description="for bugs",
            ),
        )
    finally:
        await engine.close()
    assert state.screens["bug_screen"].id == "scr-1"
    assert state.screens["bug_screen"].tab_ids == {}


@pytest.mark.asyncio
@respx.mock
async def test_delete_screen_removes_mapping():
    respx.delete(f"{CLOUD_ROOT}/screens/scr-1").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(engine, state, lambda: op.delete_screen(alias="bug_screen"))
    finally:
        await engine.close()
    assert "bug_screen" not in state.screens


@pytest.mark.asyncio
@respx.mock
async def test_add_screen_tab_records_tab_id():
    respx.post(f"{CLOUD_ROOT}/screens/scr-1/tabs", json__eq={"name": "Fields"}).mock(
        return_value=httpx.Response(201, json={"id": "tab-7"})
    )
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.add_screen_tab(
                screen_alias="bug_screen",
                tab_name="Fields",
            ),
        )
    finally:
        await engine.close()
    assert state.screens["bug_screen"].tab_ids == {"Fields": "tab-7"}


@pytest.mark.asyncio
async def test_add_screen_tab_is_idempotent_for_existing_tab_name():
    """M4 idempotency: adding a tab that's already in state returns the
    existing tab id without hitting Jira."""
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1", tab_ids={"Fields": "tab-7"})
    engine = _cloud_engine()
    captured: list[str] = []

    async def _call():
        result = await op.add_screen_tab(
            screen_alias="bug_screen",
            tab_name="Fields",
        )
        captured.append(result)

    try:
        await _run_in_ctx(engine, state, _call)
    finally:
        await engine.close()
    assert captured == ["tab-7"]
    # State unchanged
    assert state.screens["bug_screen"].tab_ids == {"Fields": "tab-7"}


@pytest.mark.asyncio
@respx.mock
async def test_add_screen_tab_field_resolves_field_alias():
    respx.post(
        f"{CLOUD_ROOT}/screens/scr-1/tabs/tab-7/fields",
        json__eq={"fieldId": "customfield_10042"},
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1", tab_ids={"Fields": "tab-7"})
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.add_screen_tab_field(
                screen_alias="bug_screen",
                tab_name="Fields",
                field_alias="bug_severity",
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_add_screen_tab_field_rejects_unknown_tab():
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError) as e:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.add_screen_tab_field(
                    screen_alias="bug_screen",
                    tab_name="Missing",
                    field_alias="bug_severity",
                ),
            )
        assert "no tab named" in str(e.value)
    finally:
        await engine.close()


# ── Screen schemes ───────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_screen_scheme_resolves_screen_aliases():
    respx.post(
        f"{CLOUD_ROOT}/screenscheme",
        json__eq={
            "name": "Bug SS",
            "description": "",
            "screens": {"default": "scr-1", "create": "scr-2"},
        },
    ).mock(return_value=httpx.Response(201, json={"id": "ss-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    state.screens["bug_create_screen"] = ScreenMapping(id="scr-2")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_screen_scheme(
                alias="bug_ss",
                name="Bug SS",
                screens={"default": "bug_screen", "create": "bug_create_screen"},
            ),
        )
    finally:
        await engine.close()
    assert state.screen_schemes["bug_ss"].id == "ss-1"


@pytest.mark.asyncio
async def test_create_screen_scheme_requires_default():
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError) as e:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_screen_scheme(
                    alias="bug_ss",
                    name="Bug SS",
                    screens={"create": "bug_screen"},
                ),
            )
        assert "'default'" in str(e.value)
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_delete_screen_scheme():
    respx.delete(f"{CLOUD_ROOT}/screenscheme/ss-1").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.screen_schemes["bug_ss"] = SimpleMapping(id="ss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(engine, state, lambda: op.delete_screen_scheme(alias="bug_ss"))
    finally:
        await engine.close()
    assert "bug_ss" not in state.screen_schemes


# ── Issue-type screen schemes ────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype_screen_scheme_resolves_default_and_typed_mappings():
    """Both 'default' and a typed issuetype alias resolve correctly."""
    respx.post(
        f"{CLOUD_ROOT}/issuetypescreenscheme",
        json__eq={
            "name": "Bug ITSS",
            "description": "",
            "issueTypeMappings": [
                {"issueTypeId": "default", "screenSchemeId": "ss-1"},
                {"issueTypeId": "10010", "screenSchemeId": "ss-2"},
            ],
        },
    ).mock(return_value=httpx.Response(201, json={"id": "itss-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    state.screen_schemes["default_ss"] = SimpleMapping(id="ss-1")
    state.screen_schemes["bug_ss"] = SimpleMapping(id="ss-2")
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype_screen_scheme(
                alias="bug_itss",
                name="Bug ITSS",
                mappings={"default": "default_ss", "bug": "bug_ss"},
            ),
        )
    finally:
        await engine.close()
    assert state.issuetype_screen_schemes["bug_itss"].id == "itss-1"


@pytest.mark.asyncio
async def test_create_issuetype_screen_scheme_requires_default():
    state = StateFile(env="dev", jira_url=BASE)
    state.screen_schemes["bug_ss"] = SimpleMapping(id="ss-1")
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_issuetype_screen_scheme(
                    alias="x",
                    name="x",
                    mappings={"bug": "bug_ss"},
                ),
            )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_delete_issuetype_screen_scheme():
    respx.delete(f"{CLOUD_ROOT}/issuetypescreenscheme/itss-1").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetype_screen_schemes["bug_itss"] = SimpleMapping(id="itss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.delete_issuetype_screen_scheme(
                alias="bug_itss",
            ),
        )
    finally:
        await engine.close()
    assert "bug_itss" not in state.issuetype_screen_schemes


# ── Field configurations ─────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_field_configuration():
    respx.post(
        f"{CLOUD_ROOT}/fieldconfiguration",
        json__eq={
            "name": "Bug FC",
            "description": "",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "fc-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_field_configuration(
                alias="bug_fc",
                name="Bug FC",
            ),
        )
    finally:
        await engine.close()
    assert state.field_configurations["bug_fc"].id == "fc-1"


@pytest.mark.asyncio
@respx.mock
async def test_create_field_configuration_retired_raises_configuration_error():
    """Classic field configurations retired on Jira Cloud in May 2026: the
    legacy endpoint replies 400 with ``Cannot create a new field
    configuration. Please use Field Scheme instead.`` We translate that
    into a ConfigurationError that points at RFC-103/104/105 (and the
    in-tree Phase 3 plan), instead of letting the raw 400 leak to the
    schema author."""
    respx.post(
        f"{CLOUD_ROOT}/fieldconfiguration",
        json__eq={
            "name": "Bug FC",
            "description": "",
        },
    ).mock(
        return_value=httpx.Response(
            400,
            json={
                "errorMessages": ["Cannot create a new field configuration. Please use Field Scheme instead."],
                "errors": {},
            },
        )
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_field_configuration(
                    alias="bug_fc",
                    name="Bug FC",
                ),
            )
    finally:
        await engine.close()
    msg = str(exc_info.value)
    assert "retired classic field configurations" in msg
    assert "RFC-103/104/105" in msg
    # Alias must NOT have been recorded: the call never created anything.
    assert "bug_fc" not in state.field_configurations


@pytest.mark.asyncio
@respx.mock
async def test_set_field_configuration_item_resolves_aliases():
    respx.put(
        f"{CLOUD_ROOT}/fieldconfiguration/fc-1/fields",
        json__eq={
            "fieldConfigurationItems": [
                {
                    "id": "customfield_10042",
                    "isRequired": True,
                    "isHidden": False,
                    "description": "Required for triage",
                }
            ]
        },
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.field_configurations["bug_fc"] = SimpleMapping(id="fc-1")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_field_configuration_item(
                fc_alias="bug_fc",
                field_alias="bug_severity",
                required=True,
                description="Required for triage",
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_delete_field_configuration():
    respx.delete(f"{CLOUD_ROOT}/fieldconfiguration/fc-1").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.field_configurations["bug_fc"] = SimpleMapping(id="fc-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.delete_field_configuration(
                alias="bug_fc",
            ),
        )
    finally:
        await engine.close()
    assert "bug_fc" not in state.field_configurations


# ── Field configuration schemes ──────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_field_configuration_scheme_creates_and_sets_mappings():
    respx.post(
        f"{CLOUD_ROOT}/fieldconfigurationscheme",
        json__eq={
            "name": "Bug FCS",
            "description": "",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "fcs-1"}))
    respx.put(
        f"{CLOUD_ROOT}/fieldconfigurationscheme/fcs-1/mapping",
        json__eq={
            "mappings": [
                {"issueTypeId": "default", "fieldConfigurationId": "fc-1"},
            ]
        },
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.field_configurations["default_fc"] = SimpleMapping(id="fc-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_field_configuration_scheme(
                alias="bug_fcs",
                name="Bug FCS",
                mappings={"default": "default_fc"},
            ),
        )
    finally:
        await engine.close()
    assert state.field_configuration_schemes["bug_fcs"].id == "fcs-1"


@pytest.mark.asyncio
@respx.mock
async def test_delete_field_configuration_scheme():
    respx.delete(f"{CLOUD_ROOT}/fieldconfigurationscheme/fcs-1").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.field_configuration_schemes["bug_fcs"] = SimpleMapping(id="fcs-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.delete_field_configuration_scheme(
                alias="bug_fcs",
            ),
        )
    finally:
        await engine.close()
    assert "bug_fcs" not in state.field_configuration_schemes


# ── Issuetypes ───────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype():
    # No same-named type exists, so create proceeds (adopt lookup returns none).
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    respx.post(
        f"{CLOUD_ROOT}/issuetype",
        json__eq={
            "name": "Bug",
            "description": "a bug",
            "type": "standard",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "10010"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype(
                alias="bug",
                name="Bug",
                description="a bug",
            ),
        )
    finally:
        await engine.close()
    assert state.issuetypes["bug"].id == "10010"


@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype_subtask_sends_subtask_type():
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    respx.post(
        f"{CLOUD_ROOT}/issuetype",
        json__eq={
            "name": "Sub-bug",
            "description": "",
            "type": "subtask",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "10011"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype(
                alias="sub_bug",
                name="Sub-bug",
                subtask=True,
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype_adopts_existing_by_name():
    """When Jira already has a same-named issue type (e.g. the built-in 'Bug'),
    create_issuetype adopts its id into state instead of POSTing a 409 (#8)."""
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "10001", "name": "Bug", "description": "default", "subtask": False},
                {"id": "10002", "name": "Task", "description": "", "subtask": False},
            ],
        )
    )
    create_route = respx.post(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(201, json={"id": "nope"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype(alias="bug", name="Bug", description="a bug"),
        )
    finally:
        await engine.close()
    assert state.issuetypes["bug"].id == "10001"
    assert not create_route.called  # adopted, no POST


@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype_ignores_project_scoped_same_name():
    """A team-managed (project-scoped) issue type with the same name must be
    ignored; only the global type is adopted. Otherwise apply 400s on the
    global endpoint with 'not a global issue type'. (#8)"""
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(
        return_value=httpx.Response(
            200,
            json=[
                # Project-scoped Bug listed first, to prove order does not matter.
                {
                    "id": "10008",
                    "name": "Bug",
                    "subtask": False,
                    "scope": {"type": "PROJECT", "project": {"id": "10001"}},
                },
                {"id": "10010", "name": "Bug", "subtask": False},  # global
            ],
        )
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype(alias="bug", name="Bug"),
        )
    finally:
        await engine.close()
    assert state.issuetypes["bug"].id == "10010"  # global, not the project-scoped 10008


@pytest.mark.asyncio
@respx.mock
async def test_delete_issuetype():
    respx.delete(f"{CLOUD_ROOT}/issuetype/10010").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(engine, state, lambda: op.delete_issuetype(alias="bug"))
    finally:
        await engine.close()
    assert "bug" not in state.issuetypes


# ── Projects ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_project_cloud_uses_leadAccountId():  # noqa: N802 (mirrors Jira's leadAccountId field)
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "1001",
                "deploymentType": "Cloud",
            },
        )
    )
    respx.post(
        f"{CLOUD_ROOT}/project",
        json__eq={
            "key": "BUG",
            "name": "Bug Tracker",
            "projectTypeKey": "software",
            "leadAccountId": "acc-123",
            "description": "",
            # CMP create now defaults gh-kanban-template: POST /project
            # silently defaults to the team-managed template without this,
            # and a TMP project ignores the whole scheme family (validated
            # on a live tenant).
            "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-kanban-template",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "p-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_project(
                alias="bug_tracker",
                key="BUG",
                name="Bug Tracker",
                project_type_key="software",
                lead="acc-123",
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_delete_project_cloud_uses_id():
    respx.delete(f"{CLOUD_ROOT}/project/p-1").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["bug_tracker"] = ProjectMapping(id="p-1", key="BUG")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.delete_project(
                alias="bug_tracker",
                key="BUG",
            ),
        )
    finally:
        await engine.close()
    assert "bug_tracker" not in state.projects


@pytest.mark.asyncio
@respx.mock
async def test_create_project_resolves_email_lead_to_account_id():
    """An email __lead__ is resolved via /user/search before create on Cloud."""
    search = respx.get(f"{CLOUD_ROOT}/user/search", params={"query": "lead@acme.com"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"accountId": "acc-999", "emailAddress": "lead@acme.com"}],
        )
    )
    respx.post(
        f"{CLOUD_ROOT}/project",
        json__eq={
            "key": "BUG",
            "name": "Bug Tracker",
            "projectTypeKey": "software",
            "leadAccountId": "acc-999",
            "description": "",
            "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-kanban-template",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "p-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_project(
                alias="bug_tracker",
                key="BUG",
                name="Bug Tracker",
                project_type_key="software",
                lead="lead@acme.com",
            ),
        )
    finally:
        await engine.close()
    assert search.called
    assert state.projects["bug_tracker"].id == "p-1"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_lead_picks_exact_email_among_many():
    """User search may return several fuzzy matches; the exact email wins."""
    respx.get(f"{CLOUD_ROOT}/user/search", params={"query": "lead@acme.com"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"accountId": "acc-other", "emailAddress": "leader@acme.com"},
                {"accountId": "acc-999", "emailAddress": "lead@acme.com"},
            ],
        )
    )
    engine = _cloud_engine()
    try:
        resolved = await engine.dialect.resolve_lead("lead@acme.com")
    finally:
        await engine.close()
    assert resolved == "acc-999"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_lead_passes_through_non_email():
    """A raw accountId/username (no @) is not sent through user search."""
    route = respx.get(f"{CLOUD_ROOT}/user/search")
    engine = _cloud_engine()
    try:
        resolved = await engine.dialect.resolve_lead("acc-123")
    finally:
        await engine.close()
    assert resolved == "acc-123"
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_resolve_lead_no_user_raises_configuration_error():
    respx.get(f"{CLOUD_ROOT}/user/search").mock(return_value=httpx.Response(200, json=[]))
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError, match="No Jira user found"):
            await engine.dialect.resolve_lead("missing@acme.com")
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_resolve_lead_permission_denied_raises_configuration_error():
    respx.get(f"{CLOUD_ROOT}/user/search").mock(
        return_value=httpx.Response(403, json={"errorMessages": ["nope"], "errors": {}})
    )
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError, match="Browse users and groups"):
            await engine.dialect.resolve_lead("lead@acme.com")
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_set_project_issuetype_screen_scheme():
    respx.put(
        f"{CLOUD_ROOT}/issuetypescreenscheme/project",
        json__eq={"issueTypeScreenSchemeId": "itss-1", "projectId": "p-1"},
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["bug_tracker"] = ProjectMapping(id="p-1", key="BUG")
    state.issuetype_screen_schemes["bug_itss"] = SimpleMapping(id="itss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_issuetype_screen_scheme(
                project_alias="bug_tracker",
                scheme_alias="bug_itss",
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_set_project_field_configuration_scheme():
    respx.put(
        f"{CLOUD_ROOT}/fieldconfigurationscheme/project",
        json__eq={"fieldConfigurationSchemeId": "fcs-1", "projectId": "p-1"},
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["bug_tracker"] = ProjectMapping(id="p-1", key="BUG")
    state.field_configuration_schemes["bug_fcs"] = SimpleMapping(id="fcs-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_field_configuration_scheme(
                project_alias="bug_tracker",
                scheme_alias="bug_fcs",
            ),
        )
    finally:
        await engine.close()


# ── IssueTypeScheme ──────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype_scheme_resolves_member_aliases_to_ids():
    """create_issuetype_scheme resolves each member alias to its Jira id,
    plus the default issuetype, and persists state.issuetype_schemes."""
    respx.post(
        f"{CLOUD_ROOT}/issuetypescheme",
        json__eq={
            "name": "Platform Issue Type Scheme",
            "description": "Auto-derived for project PLAT",
            "issueTypeIds": ["10010", "10011"],
            "defaultIssueTypeId": "10010",
        },
    ).mock(return_value=httpx.Response(201, json={"id": "its-1"}))
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.issuetypes["task"] = SimpleMapping(id="10011")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype_scheme(
                alias="PLAT_its",
                name="Platform Issue Type Scheme",
                description="Auto-derived for project PLAT",
                issuetypes=["bug", "task"],
                default_issuetype="bug",
            ),
        )
    finally:
        await engine.close()
    assert state.issuetype_schemes["PLAT_its"].id == "its-1"


@pytest.mark.asyncio
@respx.mock
async def test_create_issuetype_scheme_accepts_issueTypeSchemeId_response_shape():  # noqa: N802 (mirrors Jira's issueTypeSchemeId field)
    """Atlassian Cloud's POST /issuetypescheme returns
    {"issueTypeSchemeId": "..."} rather than the more common {"id": ...}
    shape. Surfaced by the live-Cloud smoke; pinned in a unit test so the
    next time someone narrows the response parser, this fails fast."""
    respx.post(f"{CLOUD_ROOT}/issuetypescheme").mock(
        return_value=httpx.Response(201, json={"issueTypeSchemeId": "10146"})
    )
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issuetype_scheme(
                alias="PLAT_its",
                name="Platform Issue Type Scheme",
                issuetypes=["bug"],
                default_issuetype="bug",
            ),
        )
    finally:
        await engine.close()
    assert state.issuetype_schemes["PLAT_its"].id == "10146"


@pytest.mark.asyncio
@respx.mock
async def test_set_project_issuetype_scheme():
    respx.put(
        f"{CLOUD_ROOT}/issuetypescheme/project",
        json__eq={"issueTypeSchemeId": "its-1", "projectId": "p-1"},
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(id="p-1", key="PLAT")
    state.issuetype_schemes["PLAT_its"] = SimpleMapping(id="its-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_issuetype_scheme(
                project_alias="PLAT",
                scheme_alias="PLAT_its",
            ),
        )
    finally:
        await engine.close()


# ── IssueTypeScheme membership (B3: drift must reach Jira) ───────────
@pytest.mark.asyncio
@respx.mock
async def test_update_issuetype_scheme_reconciles_membership_add_remove():
    """B3: membership drift must produce real HTTP calls. Removes the member
    Jira has but the schema dropped, and adds the declared new member. No
    move call is needed (adds append in declared order)."""
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/mapping").mock(
        return_value=httpx.Response(
            200,
            json={
                "startAt": 0,
                "isLast": True,
                "values": [
                    {"issueTypeSchemeId": "its-1", "issueTypeId": "10010"},
                    {"issueTypeSchemeId": "its-1", "issueTypeId": "10011"},
                    {"issueTypeSchemeId": "other-scheme", "issueTypeId": "10010"},
                ],
            },
        )
    )
    add = respx.put(
        f"{CLOUD_ROOT}/issuetypescheme/its-1/issuetype",
        json__eq={"issueTypeIds": ["10012"]},
    ).mock(return_value=httpx.Response(204))
    remove = respx.delete(f"{CLOUD_ROOT}/issuetypescheme/its-1/issuetype/10011").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetype_schemes["PLAT_its"] = SimpleMapping(id="its-1")
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.issuetypes["task"] = SimpleMapping(id="10012")  # declared new member
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_issuetype_scheme(
                alias="PLAT_its",
                issuetypes=["bug", "task"],
            ),
        )
    finally:
        await engine.close()
    assert add.called
    assert remove.called


@pytest.mark.asyncio
@respx.mock
async def test_update_issuetype_scheme_reorders_members_only():
    """B3: reordering ``__issuetypes__`` is reconcileable — the head member
    moves to First; the pair is then already in declared order."""
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/mapping").mock(
        return_value=httpx.Response(
            200,
            json={
                "startAt": 0,
                "isLast": True,
                "values": [
                    {"issueTypeSchemeId": "its-1", "issueTypeId": "10010"},
                    {"issueTypeSchemeId": "its-1", "issueTypeId": "10011"},
                ],
            },
        )
    )
    move = respx.put(
        f"{CLOUD_ROOT}/issuetypescheme/its-1/issuetype/move",
        json__eq={"issueTypeIds": ["10011"], "position": "First"},
    ).mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetype_schemes["PLAT_its"] = SimpleMapping(id="its-1")
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.issuetypes["task"] = SimpleMapping(id="10011")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_issuetype_scheme(
                alias="PLAT_its",
                issuetypes=["task", "bug"],
            ),
        )
    finally:
        await engine.close()
    assert move.called


@pytest.mark.asyncio
async def test_update_issuetype_scheme_rejects_default_outside_member_list():
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetype_schemes["PLAT_its"] = SimpleMapping(id="its-1")
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.issuetypes["task"] = SimpleMapping(id="10011")
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError, match="default_issuetype"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.update_issuetype_scheme(
                    alias="PLAT_its",
                    issuetypes=["bug"],
                    default_issuetype="task",
                ),
            )
    finally:
        await engine.close()
    state = StateFile(env="dev", jira_url=BASE)
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.issuetypes["task"] = SimpleMapping(id="10011")
    engine = _cloud_engine()
    try:
        with pytest.raises(Exception, match="default_issuetype"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_issuetype_scheme(
                    alias="bad_its",
                    name="Bad",
                    issuetypes=["bug", "task"],
                    default_issuetype="ghost",
                ),
            )
    finally:
        await engine.close()


# ── Idempotency: delete-when-absent is a no-op ───────────────────────
@pytest.mark.asyncio
async def test_delete_missing_alias_is_noop():
    """M4 idempotency: deleting an alias not in state is a no-op (not an error).
    This makes downgrade-of-partial safe."""
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        # Should not raise, should not hit Jira.
        await _run_in_ctx(engine, state, lambda: op.delete_screen(alias="ghost"))
    finally:
        await engine.close()
    assert "ghost" not in state.screens


# ── Security scheme ops ──────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_issue_security_scheme_records_levels_and_default():
    """The create POST carries the declared levels inline (isDefault flag ok);
    since neither create nor add-level returns level ids, a follow-up scheme
    GET fills level_ids/default_level_id into state."""
    create_route = respx.post(f"{CLOUD_ROOT}/issuesecurityschemes").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "9001",
                "levels": [
                    {"name": "Public", "description": "", "isDefault": True, "id": "lv-1"},
                ],
            },
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuesecurityschemes/9001").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 9001,
                "name": "Bug Security Scheme",
                "defaultSecurityLevelId": 10021,
                "levels": [
                    {"id": "10021", "name": "Public", "isDefault": True},
                    {"id": "10022", "name": "Internal"},
                ],
            },
        )
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issue_security_scheme(
                alias="bug_security",
                name="Bug Security Scheme",
                description="who sees",
                levels=[
                    dict(alias="level-public", name="Public", description="", is_default=True),
                    dict(alias="level-internal", name="Internal", description="", is_default=False),
                ],
            ),
        )
    finally:
        await engine.close()
    mapping = state.security_schemes["bug_security"]
    assert mapping.id == "9001"
    assert mapping.level_ids == {"level-public": "10021", "level-internal": "10022"}
    assert mapping.default_level_id == "10021"  # captured from the scheme GET's isDefault
    levels = _sent_json(create_route.calls[0])["levels"]
    assert [entry["isDefault"] for entry in levels] == [True, False]
    assert b"alias" not in create_route.calls[0].request.read()  # op-only key never reaches the wire


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_security_scheme_idempotent_rerun_fills_missing_levels():
    """Re-run: no POST; a level declared but missing from Jira's reflected
    state gets PUT (mirroring create_custom_field's option-fill)."""
    respx.put(f"{CLOUD_ROOT}/issuesecurityschemes/sc-9/level").mock(return_value=httpx.Response(204))
    respx.get(f"{CLOUD_ROOT}/issuesecurityschemes/sc-9").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 9002,
                "levels": [
                    {"id": "20001", "name": "Public"},
                    {"id": "20002", "name": "New"},
                ],
            },
        )
    )
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import SecuritySchemeMapping

    state.security_schemes["bug_security"] = SecuritySchemeMapping(id="sc-9", level_ids={"level-public": "10021"})
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_issue_security_scheme(
                alias="bug_security",
                name="Bug Security Scheme",
                levels=[
                    dict(alias="level-public", name="Public", description="", is_default=False),
                    dict(alias="level-new", name="New", description="", is_default=False),
                ],
            ),
        )
    finally:
        await engine.close()
    assert len(respx.post(f"{CLOUD_ROOT}/issuesecurityschemes").calls) == 0
    mapping = state.security_schemes["bug_security"]
    assert mapping.level_ids["level-new"] == "20002"


@pytest.mark.asyncio
@respx.mock
async def test_add_issue_security_level_member_group_and_user():
    """Groups pass the group name to the wire; user emails resolve via the
    user-search API and state records the *resolved* parameter so re-runs
    (and later diffs against reflected accountId parameters) stay idempotent."""
    put = respx.put(f"{CLOUD_ROOT}/issuesecurityschemes/sc-9/level/10022/member").mock(return_value=httpx.Response(204))
    respx.get(f"{CLOUD_ROOT}/user/search").mock(
        return_value=httpx.Response(
            200,
            json=[{"accountId": "5b10ac8b86e24b0016d46e0f", "emailAddress": "user@example.com"}],
        )
    )
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import SecuritySchemeMapping

    state.security_schemes["bug_security"] = SecuritySchemeMapping(id="sc-9", level_ids={"level-internal": "10022"})
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.add_issue_security_level_member(
                scheme_alias="bug_security",
                level_alias="level-internal",
                member_type="group",
                member_value="jira-admins",
            ),
        )
        await _run_in_ctx(
            engine,
            state,
            lambda: op.add_issue_security_level_member(
                scheme_alias="bug_security",
                level_alias="level-internal",
                member_type="user",
                member_value="user@example.com",
            ),
        )
    finally:
        await engine.close()
    assert len(respx.get(f"{CLOUD_ROOT}/user/search").calls) == 1
    bodies = [_sent_json(c) for c in put.calls]
    assert bodies[0]["members"] == [{"parameter": "jira-admins", "type": "group"}]
    assert bodies[1]["members"] == [{"parameter": "5b10ac8b86e24b0016d46e0f", "type": "user"}]
    members = state.security_schemes["bug_security"].member_values["level-internal"]
    # Declared member value resolution #  resolved accounts recorded, not emails.
    assert members == ["jira-admins", "5b10ac8b86e24b0016d46e0f"]


@pytest.mark.asyncio
@respx.mock
async def test_add_issue_security_level_member_email_rerun_is_idempotent():
    """A re-run of a member declared as an email must stop before the wire:
    state records the resolved accountId, so the declared email never
    matches it directly — the op previously fell through, re-resolved, and
    re-sent the add, which Jira rejected with "member already exists" once
    the member was in place."""
    user_search = respx.get(f"{CLOUD_ROOT}/user/search").mock(
        return_value=httpx.Response(
            200,
            json=[{"accountId": "5b10ac8b86e24b0016d46e0f", "emailAddress": "user@example.com"}],
        )
    )
    put = respx.put(f"{CLOUD_ROOT}/issuesecurityschemes/sc-9/level/10022/member").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import SecuritySchemeMapping

    state.security_schemes["bug_security"] = SecuritySchemeMapping(
        id="sc-9",
        level_ids={"level-internal": "10022"},
        # The very same member, recorded under its resolved accountId:
        member_values={"level-internal": ["5b10ac8b86e24b0016d46e0f"]},
    )
    engine = _cloud_engine()
    try:
        returned = await _run_in_ctx(
            engine,
            state,
            lambda: op.add_issue_security_level_member(
                scheme_alias="bug_security",
                level_alias="level-internal",
                member_type="user",
                member_value="user@example.com",
            ),
        )
    finally:
        await engine.close()
    assert returned == "5b10ac8b86e24b0016d46e0f"
    assert len(user_search.calls) == 1  # resolved, then recognized in state
    assert len(put.calls) == 0  # no duplicate add sent


@pytest.mark.asyncio
@respx.mock
async def test_set_default_issue_security_level_and_associate():
    default_put = respx.put(f"{CLOUD_ROOT}/issuesecurityschemes/level/default").mock(return_value=httpx.Response(204))
    associate_put = respx.put(f"{CLOUD_ROOT}/issuesecurityschemes/project").mock(
        return_value=httpx.Response(
            303,
            json={"id": "12010", "status": "COMPLETE", "self": f"{CLOUD_ROOT}/task/12010"},
        )
    )
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import ProjectMapping, SecuritySchemeMapping

    state.projects["SEC"] = ProjectMapping(id="p-99", key="SEC")
    state.security_schemes["bug_security"] = SecuritySchemeMapping(
        id="sc-9",
        level_ids={"level-public": "10021"},
        default_level_id="10021",
    )
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_default_issue_security_level(scheme_alias="bug_security", level_alias="level-public"),
        )
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_issue_security_scheme(project_alias="SEC", scheme_alias="bug_security"),
        )
    finally:
        await engine.close()
    assert _sent_json(default_put.calls[0]) == {
        "defaultValues": [{"issueSecuritySchemeId": "sc-9", "defaultLevelId": "10021"}]
    }
    assoc_body = _sent_json(associate_put.calls[0])
    assert assoc_body == {"projectId": "p-99", "schemeId": "sc-9"}
    assert state.security_schemes["bug_security"].default_level_id == "10021"


@pytest.mark.asyncio
@respx.mock
async def test_clear_default_issue_security_level_sends_null_and_forgets_state():
    default_put = respx.put(f"{CLOUD_ROOT}/issuesecurityschemes/level/default").mock(return_value=httpx.Response(204))
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import SecuritySchemeMapping

    state.security_schemes["bug_security"] = SecuritySchemeMapping(
        id="sc-9",
        level_ids={"level-public": "10021"},
        default_level_id="10021",
    )
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.clear_default_issue_security_level(scheme_alias="bug_security"),
        )
    finally:
        await engine.close()
    assert _sent_json(default_put.calls[0])["defaultValues"] == [
        {"issueSecuritySchemeId": "sc-9", "defaultLevelId": None}
    ]
    assert state.security_schemes["bug_security"].default_level_id is None


@pytest.mark.asyncio
async def test_associate_task_polls_to_failure():
    """An associate task answered ENQUEUED → poll → FAILED must raise, not
    finish green with the project left unbound."""
    from stint.exceptions import StintError

    engine = _cloud_engine()
    from stint.migrations.op import _await_security_task

    try:
        with pytest.raises(StintError):
            await _await_security_task(
                engine.dialect,
                {"status": "ENQUEUED", "self": f"{CLOUD_ROOT}/task/12010"},
            )
    finally:
        await engine.close()
