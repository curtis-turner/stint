"""Runtime schema check: engine.check(Project, state) — issue #50.

Read-only: one reflect plus one createmeta GET, then compare the declared
project against the snapshot. The dialect is stubbed (no HTTP) so the tests
exercise the comparison logic.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any, Literal, cast

import pytest
import respx

from stint import StateFile
from stint.engine import Engine
from stint.exceptions import ConfigurationError
from stint.fields import CustomField, SelectField
from stint.registry import registry
from stint.schema.issuetype import IssueType
from stint.schema.project import Project
from stint.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    SimpleMapping,
)
from stint.state.snapshot import (
    CustomFieldSnapshot,
    IssueTypeSchemeSnapshot,
    IssueTypeSnapshot,
    ProjectSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)

BASE = "https://jira.example.com"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)


severity_field = CustomField(
    alias="bug_severity",
    name="Bug Severity",
    type=SelectField,
    options=["S1", "S2"],
    description="How bad is it",
)


class Bug(IssueType):
    __alias__ = "bug"
    __title__ = "Bug"
    summary: str
    severity: Annotated[Literal["S1", "S2"], severity_field]


class Platform(Project):
    __key__ = "PLAT"
    __issuetypes__ = [Bug]


class _StubDialect:
    name = "jira_cloud"

    def __init__(self, snapshot: Any, createmeta: dict[str, set[str]]) -> None:
        self._snapshot = snapshot
        self._createmeta = createmeta
        self.reflect_calls = 0

    async def reflect(self) -> Any:
        self.reflect_calls += 1
        return self._snapshot

    async def get_createmeta(self, project_key: str) -> dict[str, Any]:
        # Return raw-shape createmeta: issuetype id -> fields dict.
        return {
            "projects": [
                {
                    "key": project_key,
                    "issuetypes": [
                        {"id": it_id, "fields": {k: {} for k in keys}} for it_id, keys in self._createmeta.items()
                    ],
                }
            ]
        }


def _engine_with(snapshot: Any, createmeta: dict[str, set[str]]) -> tuple[Engine, _StubDialect]:
    stub = _StubDialect(snapshot, createmeta)
    engine = Engine(
        base_url=BASE,
        dialect=cast("Any", stub),
        client=cast("Any", None),
    )
    return engine, stub


def _state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_10044", options={"S1": "100", "S2": "101"})
    state.issuetypes["bug"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


def _snapshot(
    *,
    severity_options: dict[str, str] | None = None,
    severity_description: str = "How bad is it",
    bug_name: str = "Bug",
    scheme_members: tuple[str, ...] = ("10010",),
    with_scheme: bool = True,
) -> Any:
    options = severity_options if severity_options is not None else {"S1": "100", "S2": "101"}
    proj = ProjectSnapshot(
        id="proj-1",
        key="PLAT",
        name="Platform",
        issuetype_scheme_id="its-1" if with_scheme else None,
    )
    return _make_snapshot(
        proj=proj,
        severity_options=options,
        severity_description=severity_description,
        bug_name=bug_name,
        scheme_members=scheme_members,
    )


def _make_snapshot(
    *, proj: Any, severity_options: dict, severity_description: str, bug_name: str, scheme_members: tuple[str, ...]
) -> Any:
    snap = Snapshot(server_info=ServerInfoSnapshot(deployment_type="Cloud", version="1000", base_url=BASE))
    snap.projects[proj.key] = proj
    snap.custom_fields["customfield_10044"] = CustomFieldSnapshot(
        id="customfield_10044",
        name="Bug Severity",
        type_id="customfieldtypes:select",
        options=dict(severity_options),
        description=severity_description,
    )
    snap.issuetypes["10010"] = IssueTypeSnapshot(id="10010", name=bug_name)
    snap.issuetype_schemes["its-1"] = IssueTypeSchemeSnapshot(id="its-1", name="s", issuetype_ids=tuple(scheme_members))
    return snap


_CREATE_FIELDS = {"10010": {"summary", "customfield_10044"}}


# ── happy path ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_in_sync_project_reports_no_errors_or_warnings():
    engine, stub = _engine_with(_snapshot(), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert report.ok
    assert report.warnings == []
    # Read-only: exactly one reflect, no writes.
    assert stub.reflect_calls == 1


@pytest.mark.asyncio
async def test_raise_for_errors_passes_when_ok():
    engine, _ = _engine_with(_snapshot(), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    report.raise_for_errors()  # must not raise


# ── errors ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_missing_custom_field_is_an_error():
    snap = _snapshot()
    del snap.custom_fields["customfield_10044"]
    engine, _ = _engine_with(snap, _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert not report.ok
    assert any("does not exist on the tenant" in e and "bug_severity" in e for e in report.errors)


@pytest.mark.asyncio
async def test_missing_option_is_an_error_naming_the_live_options():
    engine, _ = _engine_with(_snapshot(severity_options={"S1": "100"}), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert any("missing from field" in e and "'S2'" in e for e in report.errors)


@pytest.mark.asyncio
async def test_missing_issuetype_is_an_error():
    snap = _snapshot()
    del snap.issuetypes["10010"]
    engine, _ = _engine_with(snap, _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert any("does not exist on the tenant" in e for e in report.errors)


@pytest.mark.asyncio
async def test_issuetype_not_on_the_project_scheme_is_an_error():
    engine, _ = _engine_with(_snapshot(scheme_members=("99999",)), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert any("not on project PLAT's issue-type scheme" in e for e in report.errors)


@pytest.mark.asyncio
async def test_missing_project_is_an_error():
    snap = _snapshot()
    del snap.projects["PLAT"]
    engine, _ = _engine_with(snap, _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert any("does not exist on the tenant" in e for e in report.errors)


@pytest.mark.asyncio
async def test_field_not_on_the_create_screen_is_an_error():
    engine, _ = _engine_with(_snapshot(), {"10010": {"summary"}})  # no customfield
    report = await engine.check(Platform, _state())
    assert any("not on the create screen" in e and "customfield_10044" in e for e in report.errors)


@pytest.mark.asyncio
async def test_raise_for_errors_raises_a_configuration_error_naming_them():
    engine, _ = _engine_with(_snapshot(severity_options={"S1": "100"}), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    with pytest.raises(ConfigurationError) as excinfo:
        report.raise_for_errors()
    assert "PLAT" in str(excinfo.value)
    assert "'S2'" in str(excinfo.value)


# ── warnings (drift that does not block) ─────────────────────────────
@pytest.mark.asyncio
async def test_extra_option_is_a_warning_not_an_error():
    engine, _ = _engine_with(_snapshot(severity_options={"S1": "100", "S2": "101", "S3": "102"}), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert report.ok  # warnings do not block
    assert any("not declared in the schema" in w and "'S3'" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_renamed_issuetype_is_a_warning():
    engine, _ = _engine_with(_snapshot(bug_name="Defect"), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert report.ok
    assert any("renamed on the tenant" in w and "'Defect'" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_renamed_field_description_is_a_warning():
    engine, _ = _engine_with(_snapshot(severity_description="Now with more detail"), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert report.ok
    assert any("description changed" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_no_scheme_binding_skips_the_on_project_check():
    engine, _ = _engine_with(_snapshot(with_scheme=False), _CREATE_FIELDS)
    report = await engine.check(Platform, _state())
    assert report.ok  # cannot determine membership: no false error


@pytest.mark.asyncio
async def test_state_missing_issuetype_is_an_error_naming_the_fix():
    state = _state()
    del state.issuetypes["bug"]
    engine, _ = _engine_with(_snapshot(), _CREATE_FIELDS)
    report = await engine.check(Platform, state)
    assert any("not in state" in e and "stamp" in e for e in report.errors)


# ── CLI ───────────────────────────────────────────────────────────────
def test_cli_check_reports_an_unknown_schema_module():
    from stint.cli.main import main

    rc = main(["check", "--schema", "no.such.module", "--state", "/tmp/unused.yaml"])
    assert rc == 1


def test_cli_check_reports_a_schema_without_projects(tmp_path):
    schema_file = tmp_path / "schema_no_project.py"
    schema_file.write_text(
        "from stint import IssueType\n\n\nclass Plain(IssueType):\n    __alias__ = 'plain'\n",
        encoding="utf-8",
    )
    from stint.cli.main import main

    rc = main(["check", "--schema", str(schema_file), "--state", str(tmp_path / "unused.yaml")])
    assert rc == 1


# ── Cloud createmeta request shape ────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_cloud_createmeta_asks_for_the_fields_expand():
    """``expand=projects.issuetypes.fields`` is the expand Jira honors for
    embedding each issuetype's create-screen fields: a bare ``expand=fields``
    answers the issue types WITHOUT them, so every declared field reported
    "not on the create screen" while inserts setting those fields succeeded
    (live tenant, 2026-11)."""
    import httpx

    from stint import PATAuth, create_engine

    respx.get(f"{BASE}/rest/api/3/issue/createmeta").mock(return_value=httpx.Response(200, json={"projects": []}))
    engine = create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))
    try:
        await engine.dialect.get_createmeta("PLAT")
    finally:
        await engine.close()
    route = respx.get(f"{BASE}/rest/api/3/issue/createmeta")
    params = route.calls[0].request.url.params
    assert params["projectKeys"] == "PLAT"
    assert params["expand"] == "projects.issuetypes.fields"
