"""M4 polish: idempotency, advisory lock, retry/backoff, diff gaps, env config."""

import os
import sys
from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx

from pensum import (
    PATAuth,
    StateFile,
    create_engine,
    op,
)
from pensum.engine import Engine
from pensum.migrations.context import MigrationContext, reset_context, set_context
from pensum.registry import registry
from pensum.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    ScreenMapping,
    SimpleMapping,
)
from pensum.state.lock import StateLock, StateLockError

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.platform", None)


def _engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


async def _in_ctx(
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


# ──────────────────────────────────────────────────────────────────────
# Idempotency: re-running a create op with alias in state is a no-op
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_custom_field_idempotent_returns_existing_id():
    """Alias in state → returns existing id, makes no HTTP call."""
    from pensum.fields import TextField

    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10042")
    engine = _engine()
    captured: list[str] = []

    async def call():
        captured.append(
            await op.create_custom_field(
                alias="bug_severity",
                name="Severity",
                type=TextField,
            )
        )

    try:
        await _in_ctx(engine, state, call)
    finally:
        await engine.close()
    assert captured == ["customfield_10042"]
    # No POST /field was issued — respx has zero recorded calls.
    assert respx.calls.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_create_custom_field_idempotent_adds_missing_options():
    """When the field exists but the schema declares new options, the missing
    ones are added (existing ones skipped)."""
    from pensum.fields import SelectField

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
    engine = _engine()
    try:
        await _in_ctx(
            engine,
            state,
            lambda: op.create_custom_field(
                alias="bug_severity",
                name="Severity",
                type=SelectField,
                options=["S1", "S5"],
            ),
        )
    finally:
        await engine.close()
    assert state.custom_fields["bug_severity"].options == {"S1": "100", "S5": "200"}


@pytest.mark.asyncio
@respx.mock
async def test_create_screen_idempotent():
    state = StateFile(env="dev", jira_url=BASE)
    state.screens["bug_screen"] = ScreenMapping(id="scr-1")
    engine = _engine()
    captured: list[str] = []

    async def call():
        captured.append(
            await op.create_screen(
                alias="bug_screen",
                name="Bug Screen",
            )
        )

    try:
        await _in_ctx(engine, state, call)
    finally:
        await engine.close()
    assert captured == ["scr-1"]
    assert respx.calls.call_count == 0


@pytest.mark.asyncio
async def test_delete_op_idempotent_when_alias_absent():
    """Deleting a non-existent alias is a no-op (no Jira call, no error)."""
    state = StateFile(env="dev", jira_url=BASE)
    engine = _engine()
    try:
        # All seven delete ops should silently no-op:
        await _in_ctx(engine, state, lambda: op.delete_custom_field(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_screen(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_screen_scheme(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_issuetype_screen_scheme(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_field_configuration(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_field_configuration_scheme(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_issuetype(alias="ghost"))
        await _in_ctx(engine, state, lambda: op.delete_project(alias="ghost", key="GHOST"))
    finally:
        await engine.close()


# ──────────────────────────────────────────────────────────────────────
# Advisory lock
# ──────────────────────────────────────────────────────────────────────
def test_state_lock_acquires_and_releases(tmp_path):
    state_path = tmp_path / "state.yaml"
    lock = StateLock(state_path)
    lock_file = tmp_path / "state.yaml.lock"
    assert not lock_file.exists()
    lock.acquire()
    assert lock_file.exists()
    body = lock_file.read_text()
    assert f"pid={os.getpid()}" in body
    lock.release()
    assert not lock_file.exists()


def test_state_lock_refuses_when_already_held(tmp_path):
    state_path = tmp_path / "state.yaml"
    held = StateLock(state_path)
    held.acquire()
    try:
        second = StateLock(state_path)
        with pytest.raises(StateLockError) as e:
            second.acquire()
        assert f"pid={os.getpid()}" in str(e.value)
    finally:
        held.release()


def test_state_lock_context_manager_releases_on_exception(tmp_path):
    state_path = tmp_path / "state.yaml"
    lock_file = tmp_path / "state.yaml.lock"
    with pytest.raises(RuntimeError):
        with StateLock(state_path):
            assert lock_file.exists()
            raise RuntimeError("boom")
    assert not lock_file.exists()


# ──────────────────────────────────────────────────────────────────────
# HTTP retry/backoff
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success():
    """First response 429, second 200 — request succeeds after one retry."""
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(
                200,
                json={
                    "baseUrl": BASE,
                    "version": "9",
                    "deploymentType": "Cloud",
                },
            ),
        ]
    )
    engine = _engine()
    try:
        info = await engine.detect()
    finally:
        await engine.close()
    assert info is True
    assert respx.calls.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_503_then_success():
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "0"}),
            httpx.Response(503, headers={"Retry-After": "0"}),
            httpx.Response(
                200,
                json={
                    "baseUrl": BASE,
                    "version": "9",
                    "deploymentType": "Server",
                },
            ),
        ]
    )
    engine = _engine()
    try:
        await engine.detect()
    finally:
        await engine.close()
    assert respx.calls.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_retry_gives_up_after_max_retries():
    """4 consecutive 429s (default max_retries=3) → TransportError."""
    from pensum.exceptions import TransportError

    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}),
    )
    engine = _engine()
    try:
        with pytest.raises(TransportError):
            await engine.detect()
    finally:
        await engine.close()
    # Original + 3 retries = 4 calls.
    assert respx.calls.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_no_retry_on_non_retryable_status():
    """4xx other than 429 propagates immediately (no retry)."""
    from pensum.exceptions import NotFoundError

    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(404),
    )
    engine = _engine()
    try:
        with pytest.raises(NotFoundError):
            await engine.detect()
    finally:
        await engine.close()
    assert respx.calls.call_count == 1


# ──────────────────────────────────────────────────────────────────────
# Diff: project rename
# ──────────────────────────────────────────────────────────────────────
def test_diff_emits_update_project_on_rename():
    import examples.platform  # noqa: F401  -- imports trigger registration
    from pensum.autogen.desired import build_desired_snapshot
    from pensum.autogen.diff import UpdateProject, diff
    from pensum.state.snapshot import (
        ProjectSnapshot,
        ServerInfoSnapshot,
        Snapshot,
    )

    desired = build_desired_snapshot()
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(id="p-1", key="PLAT")

    snapshot = Snapshot(
        server_info=ServerInfoSnapshot(
            deployment_type="Server",
            version="9",
            base_url="x",
        )
    )
    snapshot.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="Platform (Old Name)",
    )
    result = diff(
        desired=desired,
        snapshot=snapshot,
        state=state,
        allow_delete=False,
    )
    updates = [c for c in result.changes if isinstance(c, UpdateProject)]
    assert len(updates) == 1
    assert updates[0].alias == "PLAT"
    assert updates[0].name == "Platform"  # __title__ defaults to class name


# ──────────────────────────────────────────────────────────────────────
# Diff: ITSS mapping rebind
# ──────────────────────────────────────────────────────────────────────
def test_diff_emits_update_itss_when_mappings_change():
    """Existing ITSS in state has mapping default→ss-A, but schema wants
    default→ss-B (synthesized). Diff emits UpdateIssueTypeScreenScheme
    with new mappings."""
    from pensum.autogen.desired import (
        DesiredIssueTypeScreenScheme,
        DesiredScreenScheme,
        DesiredSnapshot,
    )
    from pensum.autogen.diff import UpdateIssueTypeScreenScheme, diff
    from pensum.state.snapshot import (
        IssueTypeScreenSchemeMappingSnapshot,
        IssueTypeScreenSchemeSnapshot,
        ServerInfoSnapshot,
        Snapshot,
    )

    desired = DesiredSnapshot()
    desired.screen_schemes["ss_b"] = DesiredScreenScheme(
        alias="ss_b",
        name="SS B",
        description="",
        screens={"default": "anywhere"},
    )
    desired.issuetype_screen_schemes["plat_itss"] = DesiredIssueTypeScreenScheme(
        alias="plat_itss",
        name="PLAT ITSS",
        description="",
        mappings={"default": "ss_b"},
    )

    state = StateFile(env="dev", jira_url=BASE)
    state.screen_schemes["ss_a"] = SimpleMapping(id="ss-old")
    state.screen_schemes["ss_b"] = SimpleMapping(id="ss-new")
    state.issuetype_screen_schemes["plat_itss"] = SimpleMapping(id="itss-1")

    snap = Snapshot(
        server_info=ServerInfoSnapshot(
            deployment_type="Server",
            version="9",
            base_url="x",
        )
    )
    snap.issuetype_screen_schemes["itss-1"] = IssueTypeScreenSchemeSnapshot(
        id="itss-1",
        name="PLAT ITSS",
        mappings=(
            IssueTypeScreenSchemeMappingSnapshot(
                issuetype_id="default",
                screen_scheme_id="ss-old",
            ),
        ),
    )
    result = diff(desired=desired, snapshot=snap, state=state, allow_delete=False)
    updates = [c for c in result.changes if isinstance(c, UpdateIssueTypeScreenScheme)]
    assert len(updates) == 1
    assert updates[0].mappings == {"default": "ss_b"}


# ──────────────────────────────────────────────────────────────────────
# Env config loader
# ──────────────────────────────────────────────────────────────────────
def test_env_config_fills_in_missing_flags(tmp_path, monkeypatch):
    """When ~/.pensum/envs/prod.yaml exists, --env prod fills in url/auth."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "prod.yaml").write_text("url: https://jira.example.com\nauth: pat\ndialect: jira_cloud\n")
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))

    import argparse

    from pensum.cli.env_config import apply_env_defaults

    args = argparse.Namespace(
        env="prod",
        url=None,
        auth=None,
        dialect=None,
        token_env="PENSUM_TOKEN",
        user_env="PENSUM_USER",
        no_verify_ssl=False,
    )
    apply_env_defaults(args, args.env)
    assert args.url == "https://jira.example.com"
    assert args.auth == "pat"
    assert args.dialect == "jira_cloud"


def test_env_config_explicit_flags_override(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "prod.yaml").write_text("url: https://from-config\n")
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))

    import argparse

    from pensum.cli.env_config import apply_env_defaults

    args = argparse.Namespace(
        env="prod",
        url="https://from-cli",
        auth="pat",
        dialect=None,
        token_env="PENSUM_TOKEN",
        user_env="PENSUM_USER",
        no_verify_ssl=False,
    )
    apply_env_defaults(args, args.env)
    assert args.url == "https://from-cli"  # CLI wins


def test_env_config_missing_file_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(tmp_path))
    import argparse

    from pensum.cli.env_config import apply_env_defaults

    args = argparse.Namespace(
        env="prod",
        url="x",
        auth="pat",
        dialect=None,
        token_env="PENSUM_TOKEN",
        user_env="PENSUM_USER",
        no_verify_ssl=False,
    )
    apply_env_defaults(args, "prod")  # config does not exist → no-op
    assert args.url == "x"


def test_env_config_rejects_unknown_keys(tmp_path, monkeypatch):
    from pensum.exceptions import ConfigurationError

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "prod.yaml").write_text("url: x\nnonsense: 1\n")
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))

    from pensum.cli.env_config import load_env_config

    with pytest.raises(ConfigurationError) as e:
        load_env_config("prod")
    assert "nonsense" in str(e.value)


# ──────────────────────────────────────────────────────────────────────
# cmd_upgrade._resolve_connection: env-config token_env / user_env (issue #2)
# ──────────────────────────────────────────────────────────────────────
def test_resolve_connection_reads_token_and_user_env_from_yaml(tmp_path, monkeypatch):
    """YAML's token_env / user_env keys are honored when the CLI doesn't pass them."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "cloud.yaml").write_text(
        "url: https://x.atlassian.net\n"
        "auth: api-token\n"
        "dialect: jira_cloud\n"
        "token_env: PENSUM_CLOUD_TOKEN\n"
        "user_env: PENSUM_CLOUD_EMAIL\n"
    )
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))
    from pensum.cli.env_config import resolve_connection

    url, auth, dialect, token_env, user_env, no_verify_ssl = resolve_connection(
        env="cloud",
        url=None,
        auth=None,
        dialect=None,
        token_env=None,
        user_env=None,
        no_verify_ssl=False,
    )
    assert url == "https://x.atlassian.net"
    assert auth == "api-token"
    assert token_env == "PENSUM_CLOUD_TOKEN"
    assert user_env == "PENSUM_CLOUD_EMAIL"


def test_resolve_connection_explicit_cli_overrides_yaml(tmp_path, monkeypatch):
    """When the CLI passes --token-env/--user-env, those win over the YAML."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "cloud.yaml").write_text(
        "url: https://x.atlassian.net\nauth: api-token\ntoken_env: FROM_YAML_T\nuser_env: FROM_YAML_U\n"
    )
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))
    from pensum.cli.env_config import resolve_connection

    _, _, _, token_env, user_env, _ = resolve_connection(
        env="cloud",
        url=None,
        auth=None,
        dialect=None,
        token_env="FROM_CLI_T",
        user_env="FROM_CLI_U",
        no_verify_ssl=False,
    )
    assert token_env == "FROM_CLI_T"
    assert user_env == "FROM_CLI_U"


def test_resolve_connection_falls_back_to_defaults_when_unset(tmp_path, monkeypatch):
    """No YAML token_env/user_env and no CLI override → defaults apply."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "cloud.yaml").write_text("url: https://x.atlassian.net\nauth: api-token\n")
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))
    from pensum.cli.env_config import resolve_connection

    _, _, _, token_env, user_env, _ = resolve_connection(
        env="cloud",
        url=None,
        auth=None,
        dialect=None,
        token_env=None,
        user_env=None,
        no_verify_ssl=False,
    )
    assert token_env == "PENSUM_TOKEN"
    assert user_env == "PENSUM_USER"


def test_resolve_connection_defaults_when_no_yaml_at_all(tmp_path, monkeypatch):
    """Missing YAML file → CLI defaults still flow through as concrete strings."""
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(tmp_path))  # empty dir
    # Use a unique env name so the user's ~/.pensum/envs/ fallback doesn't match.
    from pensum.cli.env_config import resolve_connection

    url, auth, dialect, token_env, user_env, _ = resolve_connection(
        env="pytest-no-such-env",
        url=None,
        auth=None,
        dialect=None,
        token_env=None,
        user_env=None,
        no_verify_ssl=False,
    )
    assert url is None and auth is None and dialect is None
    assert token_env == "PENSUM_TOKEN"
    assert user_env == "PENSUM_USER"


# ──────────────────────────────────────────────────────────────────────
# CLI: upgrade reports missing connection cleanly
# ──────────────────────────────────────────────────────────────────────
def test_cli_upgrade_errors_when_url_and_auth_missing(tmp_path, monkeypatch):
    """No env config, no --url, no --auth → clean SystemExit with the offending keys."""
    from pensum.cli.main import main

    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(tmp_path / "nonexistent"))
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    with pytest.raises(SystemExit) as e:
        main(
            [
                "upgrade",
                "--migrations-dir",
                str(mig_dir),
                "--state",
                str(state_path),
                "--env",
                "dev",
            ]
        )
    assert "url" in str(e.value)
    assert "auth" in str(e.value)


# ──────────────────────────────────────────────────────────────────────
# CLI: reflect / stamp / revision honor --env (issue #1)
# ──────────────────────────────────────────────────────────────────────
def test_cli_reflect_reads_connection_from_env_config(tmp_path, monkeypatch, capsys):
    """`pensum reflect --env cloud` resolves url/auth/dialect from YAML and runs."""
    import httpx
    import respx
    import yaml as _yaml

    from pensum.cli.main import main

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "dev.yaml").write_text(
        "url: jira_cloud+https://jira.example.com\nauth: pat\ndialect: jira_cloud\ntoken_env: PENSUM_DEV_TOKEN\n"
    )
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("PENSUM_DEV_TOKEN", "test-pat-from-env-config")

    base = "https://jira.example.com"
    dc_root = f"{base}/rest/api/3"

    def _paginated(values: list) -> dict:
        return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{dc_root}/serverInfo").mock(
            return_value=httpx.Response(
                200,
                json={"baseUrl": base, "version": "9.12.4", "deploymentType": "Server"},
            )
        )
        for path in (f"{dc_root}/field", f"{dc_root}/issuetype"):
            mock.get(path).mock(return_value=httpx.Response(200, json=[]))
        for path in (
            f"{dc_root}/project/search",
            f"{dc_root}/screens",
            f"{dc_root}/screenscheme",
            f"{dc_root}/issuetypescheme",
            f"{dc_root}/issuetypescheme/mapping",
            f"{dc_root}/issuetypescheme/project",
            f"{dc_root}/issuetypescreenscheme",
            f"{dc_root}/issuetypescreenscheme/project",
            f"{dc_root}/fieldconfiguration",
            f"{dc_root}/fieldconfigurationscheme",
            f"{dc_root}/fieldconfigurationscheme/project",
        ):
            mock.get(path).mock(return_value=httpx.Response(200, json=_paginated([])))

        rc = main(["reflect", "--env", "dev"])

    assert rc == 0
    parsed = _yaml.safe_load(capsys.readouterr().out)
    assert parsed["server_info"]["version"] == "9.12.4"


def test_cli_reflect_errors_with_missing_connection_and_no_env(tmp_path, monkeypatch):
    """No --env, no --url, no --auth → SystemExit listing both missing keys."""
    from pensum.cli.main import main

    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(tmp_path / "nonexistent"))
    with pytest.raises(SystemExit) as e:
        main(["reflect"])
    assert "url" in str(e.value)
    assert "auth" in str(e.value)


def test_cli_stamp_reads_connection_from_env_config(tmp_path, monkeypatch):
    """`pensum stamp --env dev` resolves url/auth/dialect from YAML."""
    import httpx
    import respx

    from pensum.cli.main import main

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "dev.yaml").write_text("url: jira_cloud+https://jira.example.com\nauth: pat\ndialect: jira_cloud\n")
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("PENSUM_TOKEN", "test-pat")

    schema_path = tmp_path / "schema.py"
    schema_path.write_text("# empty schema\n")

    base = "https://jira.example.com"
    dc_root = f"{base}/rest/api/3"

    def _paginated(values: list) -> dict:
        return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{dc_root}/serverInfo").mock(
            return_value=httpx.Response(200, json={"baseUrl": base, "version": "9.12.4", "deploymentType": "Server"})
        )
        for path in (f"{dc_root}/field", f"{dc_root}/issuetype"):
            mock.get(path).mock(return_value=httpx.Response(200, json=[]))
        for path in (
            f"{dc_root}/project/search",
            f"{dc_root}/screens",
            f"{dc_root}/screenscheme",
            f"{dc_root}/issuetypescheme",
            f"{dc_root}/issuetypescheme/mapping",
            f"{dc_root}/issuetypescheme/project",
            f"{dc_root}/issuetypescreenscheme",
            f"{dc_root}/issuetypescreenscheme/project",
            f"{dc_root}/fieldconfiguration",
            f"{dc_root}/fieldconfigurationscheme",
            f"{dc_root}/fieldconfigurationscheme/project",
        ):
            mock.get(path).mock(return_value=httpx.Response(200, json=_paginated([])))

        rc = main(
            [
                "stamp",
                "--schema",
                str(schema_path),
                "--state",
                str(tmp_path / "state.yaml"),
                "--env",
                "dev",
            ]
        )

    assert rc == 0
    assert (tmp_path / "state.yaml").exists()


def test_cli_revision_autogenerate_reads_connection_from_env_config(tmp_path, monkeypatch, capsys):
    """`pensum revision --autogenerate --env dev` no longer requires --url/--auth on the CLI."""
    import httpx
    import respx

    from pensum.cli.main import main

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "dev.yaml").write_text("url: jira_cloud+https://jira.example.com\nauth: pat\ndialect: jira_cloud\n")
    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("PENSUM_TOKEN", "test-pat")

    schema_path = tmp_path / "schema.py"
    schema_path.write_text("# empty schema\n")

    base = "https://jira.example.com"
    dc_root = f"{base}/rest/api/3"

    def _paginated(values: list) -> dict:
        return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{dc_root}/serverInfo").mock(
            return_value=httpx.Response(200, json={"baseUrl": base, "version": "9.12.4", "deploymentType": "Server"})
        )
        for path in (f"{dc_root}/field", f"{dc_root}/issuetype"):
            mock.get(path).mock(return_value=httpx.Response(200, json=[]))
        for path in (
            f"{dc_root}/project/search",
            f"{dc_root}/screens",
            f"{dc_root}/screenscheme",
            f"{dc_root}/issuetypescheme",
            f"{dc_root}/issuetypescheme/mapping",
            f"{dc_root}/issuetypescheme/project",
            f"{dc_root}/issuetypescreenscheme",
            f"{dc_root}/issuetypescreenscheme/project",
            f"{dc_root}/fieldconfiguration",
            f"{dc_root}/fieldconfigurationscheme",
            f"{dc_root}/fieldconfigurationscheme/project",
        ):
            mock.get(path).mock(return_value=httpx.Response(200, json=_paginated([])))

        rc = main(
            [
                "revision",
                "--migrations-dir",
                str(tmp_path / "migrations"),
                "--message",
                "test",
                "--autogenerate",
                "--schema",
                str(schema_path),
                "--state",
                str(tmp_path / "state.yaml"),
                "--env",
                "dev",
            ]
        )

    assert rc == 0
    assert "no changes detected" in capsys.readouterr().out


def test_cli_revision_autogenerate_errors_with_missing_connection(tmp_path, monkeypatch):
    """Autogen with --env but no YAML and no --url/--auth → SystemExit."""
    from pensum.cli.main import main

    monkeypatch.setenv("PENSUM_CONFIG_DIR", str(tmp_path / "nonexistent"))
    schema_path = tmp_path / "schema.py"
    schema_path.write_text("# empty\n")
    with pytest.raises(SystemExit) as e:
        main(
            [
                "revision",
                "--migrations-dir",
                str(tmp_path / "migrations"),
                "--message",
                "test",
                "--autogenerate",
                "--schema",
                str(schema_path),
                "--state",
                str(tmp_path / "state.yaml"),
                "--env",
                "dev",
            ]
        )
    assert "url" in str(e.value)
    assert "auth" in str(e.value)
