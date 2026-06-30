"""M2 polish: mid-op state persistence and update_* ops."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import respx

from stint import PATAuth, StateFile, create_engine, op
from stint.engine import Engine
from stint.exceptions import ConfigurationError, TransportError
from stint.fields import SelectField
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.state.file import (
    CustomFieldMapping,
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
    body: Callable[[], Awaitable[object]],
    *,
    state_path: Path | None = None,
) -> None:
    ctx = MigrationContext(
        engine=engine,
        state=state,
        direction="upgrade",
        state_path=state_path,
    )
    token = set_context(ctx)
    try:
        await body()
    finally:
        reset_context(token)


# ── Mid-op state persistence ─────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_custom_field_persists_parent_before_options(tmp_path):
    """Option 3 of 4 fails. Disk state shows field + options 1, 2 — NOT all-or-nothing."""
    respx.post(f"{CLOUD_ROOT}/field").mock(return_value=httpx.Response(201, json={"id": "customfield_10042"}))
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"id": "ctx-1"}], "isLast": True, "startAt": 0, "maxResults": 1},
        )
    )
    respx.post(
        f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
        json__eq={"options": [{"value": "S1"}]},
    ).mock(return_value=httpx.Response(200, json={"options": [{"id": "100", "value": "S1"}]}))
    respx.post(
        f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
        json__eq={"options": [{"value": "S2"}]},
    ).mock(return_value=httpx.Response(200, json={"options": [{"id": "101", "value": "S2"}]}))
    respx.post(
        f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
        json__eq={"options": [{"value": "S3"}]},
    ).mock(return_value=httpx.Response(500, json={"errorMessages": ["boom"]}))

    state = StateFile(env="dev", jira_url=BASE)
    state_path = tmp_path / "state.yaml"
    engine = _cloud_engine()
    try:
        with pytest.raises(TransportError):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_custom_field(
                    alias="bug_severity",
                    name="Severity",
                    type=SelectField,
                    options=["S1", "S2", "S3", "S4"],
                ),
                state_path=state_path,
            )
    finally:
        await engine.close()

    on_disk = StateFile.load(state_path)
    assert on_disk.custom_fields["bug_severity"].id == "customfield_10042"
    assert on_disk.custom_fields["bug_severity"].options == {"S1": "100", "S2": "101"}


@pytest.mark.asyncio
@respx.mock
async def test_create_fcs_persists_id_before_mappings_put(tmp_path):
    """If POST FCS succeeds but PUT mappings fails, disk state must show the
    FCS id so a re-run won't duplicate it."""
    respx.post(f"{CLOUD_ROOT}/fieldconfigurationscheme").mock(return_value=httpx.Response(201, json={"id": "fcs-1"}))
    respx.put(f"{CLOUD_ROOT}/fieldconfigurationscheme/fcs-1/mapping").mock(
        return_value=httpx.Response(500, json={"errorMessages": ["boom"]})
    )

    state = StateFile(env="dev", jira_url=BASE)
    state.field_configurations["default_fc"] = SimpleMapping(id="fc-1")
    state_path = tmp_path / "state.yaml"
    engine = _cloud_engine()
    try:
        with pytest.raises(TransportError):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_field_configuration_scheme(
                    alias="bug_fcs",
                    name="Bug FCS",
                    mappings={"default": "default_fc"},
                ),
                state_path=state_path,
            )
    finally:
        await engine.close()

    on_disk = StateFile.load(state_path)
    assert on_disk.field_configuration_schemes["bug_fcs"].id == "fcs-1"


@pytest.mark.asyncio
async def test_persist_is_noop_without_state_path():
    """Tests that exercise ops in-memory (no state_path) still work — ctx.persist
    silently no-ops when state_path is None."""
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _cloud_engine()
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
                return_value=httpx.Response(
                    200,
                    json={"values": [{"id": "ctx-1"}], "isLast": True, "startAt": 0, "maxResults": 1},
                )
            )
            router.post(
                f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
            ).mock(return_value=httpx.Response(200, json={"options": [{"id": "200", "value": "S5"}]}))
            await _run_in_ctx(
                engine,
                state,
                lambda: op.add_custom_field_option(
                    field_alias="bug_severity",
                    value="S5",
                ),
            )
    finally:
        await engine.close()
    assert state.custom_fields["bug_severity"].options == {"S5": "200"}


# ── update_custom_field ──────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_update_custom_field_sends_only_provided_fields():
    respx.put(
        f"{CLOUD_ROOT}/field/customfield_10042",
        json__eq={
            "name": "Bug Severity (renamed)",
        },
    ).mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_custom_field(
                alias="bug_severity",
                name="Bug Severity (renamed)",
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_update_custom_field_with_no_changes_is_noop():
    """No HTTP fires if all update kwargs are None."""
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_custom_field(
                alias="bug_severity",
            ),
        )
    finally:
        await engine.close()
    assert not respx.routes


# ── add/remove option ────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_add_custom_field_option_appends_to_state():
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"id": "ctx-1"}], "isLast": True, "startAt": 0, "maxResults": 1},
        )
    )
    respx.post(
        f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option",
        json__eq={"options": [{"value": "S5"}]},
    ).mock(return_value=httpx.Response(200, json={"options": [{"id": "200", "value": "S5"}]}))

    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100"},
    )
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.add_custom_field_option(
                field_alias="bug_severity",
                value="S5",
            ),
        )
    finally:
        await engine.close()
    assert state.custom_fields["bug_severity"].options == {"S1": "100", "S5": "200"}


@pytest.mark.asyncio
async def test_add_custom_field_option_rejects_duplicate_value():
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100"},
    )
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError) as e:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.add_custom_field_option(
                    field_alias="bug_severity",
                    value="S1",
                ),
            )
        assert "already has option" in str(e.value)
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_remove_custom_field_option_cloud_goes_through_context():
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
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": [{"id": "ctx-1"}],
                "isLast": True,
                "startAt": 0,
                "maxResults": 1,
            },
        )
    )
    respx.delete(f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option/100").mock(
        return_value=httpx.Response(204)
    )

    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100"},
    )
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.remove_custom_field_option(
                field_alias="bug_severity",
                value="S1",
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_remove_custom_field_option_missing_value_raises():
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(
        id="customfield_10042",
        options={"S1": "100"},
    )
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError) as e:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.remove_custom_field_option(
                    field_alias="bug_severity",
                    value="S9",
                ),
            )
        assert "no option named" in str(e.value)
    finally:
        await engine.close()


# ── update_screen ────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_update_screen_rename():
    respx.put(
        f"{CLOUD_ROOT}/screens/scr-1",
        json__eq={
            "name": "Bug Screen v2",
        },
    ).mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_screen(
                alias="bug_screen",
                name="Bug Screen v2",
            ),
        )
    finally:
        await engine.close()


# ── update_screen_scheme ─────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_update_screen_scheme_replaces_screens_dict():
    respx.put(
        f"{CLOUD_ROOT}/screenscheme/ss-1",
        json__eq={
            "screens": {"default": "scr-2", "create": "scr-1"},
        },
    ).mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE)
    state.screens["screen_a"] = ScreenMapping(id="scr-1")
    state.screens["screen_b"] = ScreenMapping(id="scr-2")
    state.screen_schemes["bug_ss"] = SimpleMapping(id="ss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_screen_scheme(
                alias="bug_ss",
                screens={"default": "screen_b", "create": "screen_a"},
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_update_screen_scheme_requires_default_when_screens_given():
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["screen_a"] = ScreenMapping(id="scr-1")
    state.screen_schemes["bug_ss"] = SimpleMapping(id="ss-1")
    engine = _cloud_engine()
    try:
        with pytest.raises(ConfigurationError):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.update_screen_scheme(
                    alias="bug_ss",
                    screens={"create": "screen_a"},
                ),
            )
    finally:
        await engine.close()


# ── update_issuetype_screen_scheme ───────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_update_issuetype_screen_scheme_replaces_mappings():
    respx.put(
        f"{CLOUD_ROOT}/issuetypescreenscheme/itss-1/mapping",
        json__eq={
            "issueTypeMappings": [
                {"issueTypeId": "default", "screenSchemeId": "ss-1"},
                {"issueTypeId": "10010", "screenSchemeId": "ss-2"},
            ]
        },
    ).mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE)
    state.screen_schemes["default_ss"] = SimpleMapping(id="ss-1")
    state.screen_schemes["bug_ss"] = SimpleMapping(id="ss-2")
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.issuetype_screen_schemes["bug_itss"] = SimpleMapping(id="itss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_issuetype_screen_scheme(
                alias="bug_itss",
                mappings={"default": "default_ss", "bug": "bug_ss"},
            ),
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_update_issuetype_screen_scheme_rename_only_skips_mappings_put():
    respx.put(
        f"{CLOUD_ROOT}/issuetypescreenscheme/itss-1",
        json__eq={
            "name": "Renamed ITSS",
        },
    ).mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE)
    state.issuetype_screen_schemes["bug_itss"] = SimpleMapping(id="itss-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_issuetype_screen_scheme(
                alias="bug_itss",
                name="Renamed ITSS",
            ),
        )
    finally:
        await engine.close()


# ── update_field_configuration_scheme ────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_update_field_configuration_scheme_replaces_mappings():
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
    state.field_configuration_schemes["bug_fcs"] = SimpleMapping(id="fcs-1")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_field_configuration_scheme(
                alias="bug_fcs",
                mappings={"default": "default_fc"},
            ),
        )
    finally:
        await engine.close()


# ── update_issuetype ─────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_update_issuetype_rename_and_description():
    respx.put(
        f"{CLOUD_ROOT}/issuetype/10010",
        json__eq={
            "name": "Defect",
            "description": "production defect",
        },
    ).mock(return_value=httpx.Response(204))

    state = StateFile(env="dev", jira_url=BASE)
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.update_issuetype(
                alias="bug",
                name="Defect",
                description="production defect",
            ),
        )
    finally:
        await engine.close()
