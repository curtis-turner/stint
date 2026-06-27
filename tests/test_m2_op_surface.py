"""M2 expanded op surface: every op in stint.op, end-to-end via respx.

Each test sets up the smallest state preconditions, runs the op inside a
fresh MigrationContext, and asserts on Jira HTTP calls plus state mutations.
The runner is exercised by test_m2_migrations.py; tests here focus on the
op-level alias resolution and dialect contract.
"""

from collections.abc import Awaitable, Callable

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


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


async def _run_in_ctx(
    engine: Engine,
    state: StateFile,
    body: Callable[[], Awaitable[None]],
) -> None:
    ctx = MigrationContext(engine=engine, state=state, direction="upgrade")
    token = set_context(ctx)
    try:
        await body()
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


@pytest.mark.asyncio
async def test_create_issuetype_scheme_rejects_default_outside_member_list():
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
