"""M7 partial: team-managed project support.

What's in scope here:

- ``ProjectMapping`` carries ``style`` and ``key``; YAML round-trip preserves both.
- ``op.create_project`` records ``style`` in state and defaults the
  ``project_template_key`` for team-managed creates.
- ``set_project_*_scheme`` ops refuse to run on team-managed projects and
  surface a Jira UI deep link via ``UnsupportedTMPOpError``.
- Stamp captures ``style`` from Jira and back-fills it on pre-M7 state files.
- Diff warns (no Change emitted) when schema style and state style disagree.
- ``DesiredProject`` reads ``__style__`` from the Project class.
- Project metaclass enforces style/scheme combinations.
"""

import sys
from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx

from pensum import PATAuth, StateFile, create_engine, op
from pensum.autogen.desired import build_desired_snapshot
from pensum.autogen.diff import UpdateProject, diff
from pensum.autogen.stamp import stamp
from pensum.engine import Engine
from pensum.exceptions import ConfigurationError, UnsupportedTMPOpError
from pensum.migrations.context import MigrationContext, reset_context, set_context
from pensum.registry import registry
from pensum.state.file import ProjectMapping, SimpleMapping
from pensum.state.snapshot import (
    ProjectSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)

BASE = "https://jira.example.com"
DC_ROOT = f"{BASE}/rest/api/2"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.platform", None)


def _dc_engine() -> Engine:
    return create_engine(f"jira_dc+{BASE}", auth=PATAuth("tok"))


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


def _empty_snapshot() -> Snapshot:
    return Snapshot(
        server_info=ServerInfoSnapshot(
            deployment_type="Server",
            version="9",
            base_url="x",
        )
    )


# ── ProjectMapping YAML round-trip ───────────────────────────────────
def test_project_mapping_yaml_default_style_omits_field():
    """company-managed is the default; YAML should stay tidy and not write it."""
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["BUG"] = ProjectMapping(id="p-1", key="BUG")
    text = state.to_yaml()
    # style default is implicit
    assert "style:" not in text
    assert "key: BUG" in text


def test_project_mapping_yaml_team_managed_writes_style():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["BUG"] = ProjectMapping(id="p-1", style="team-managed", key="BUG")
    text = state.to_yaml()
    assert "style: team-managed" in text


def test_project_mapping_yaml_round_trip_preserves_style_and_key():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["cmp"] = ProjectMapping(id="p-1", key="CMP")
    state.projects["tmp"] = ProjectMapping(id="p-2", style="team-managed", key="TMP")
    loaded = StateFile.from_yaml(state.to_yaml())
    assert loaded.projects["cmp"].style == "company-managed"
    assert loaded.projects["cmp"].key == "CMP"
    assert loaded.projects["tmp"].style == "team-managed"
    assert loaded.projects["tmp"].key == "TMP"


def test_project_mapping_from_dict_defaults_style():
    """A state file written before M7 has no style; load must default it."""
    pre_m7 = {"id": "p-1"}
    pm = ProjectMapping.from_dict(pre_m7)
    assert pm.style == "company-managed"
    assert pm.key == ""


# ── op.create_project records style ──────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_create_project_records_default_style():
    respx.post(f"{DC_ROOT}/project").mock(
        return_value=httpx.Response(201, json={"id": "p-1"}),
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _dc_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_project(
                alias="bug_tracker",
                key="BUG",
                name="Bug Tracker",
                project_type_key="software",
                lead="jdoe",
            ),
        )
    finally:
        await engine.close()
    assert state.projects["bug_tracker"].style == "company-managed"
    assert state.projects["bug_tracker"].key == "BUG"


@pytest.mark.asyncio
@respx.mock
async def test_create_project_team_managed_defaults_template():
    """TMP creates need a projectTemplateKey; create_project picks the
    next-gen Kanban template when the caller omits it."""
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
    route = respx.post(f"{CLOUD_ROOT}/project").mock(
        return_value=httpx.Response(201, json={"id": "p-1"}),
    )
    state = StateFile(env="dev", jira_url=BASE)
    engine = _cloud_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.create_project(
                alias="growth",
                key="GRW",
                name="Growth",
                project_type_key="software",
                lead="acc-1",
                style="team-managed",
            ),
        )
    finally:
        await engine.close()
    body = route.calls.last.request.read().decode()
    assert "gh-simplified-agility-kanban" in body
    assert state.projects["growth"].style == "team-managed"


@pytest.mark.asyncio
async def test_create_project_rejects_unknown_style():
    state = StateFile(env="dev", jira_url=BASE)
    engine = _dc_engine()
    try:
        with pytest.raises(ConfigurationError, match="unknown style"):
            await _run_in_ctx(
                engine,
                state,
                lambda: op.create_project(
                    alias="x",
                    key="X",
                    name="X",
                    project_type_key="software",
                    lead="jdoe",
                    style="next-gen",  # snapshot vocabulary, not pensum's
                ),
            )
    finally:
        await engine.close()


# ── TMP guard: set_project_*_scheme refuses team-managed ─────────────
@pytest.mark.asyncio
async def test_set_project_issuetype_screen_scheme_refuses_tmp():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["growth"] = ProjectMapping(
        id="p-1",
        style="team-managed",
        key="GRW",
    )
    state.issuetype_screen_schemes["grw_itss"] = SimpleMapping(id="itss-1")
    engine = _dc_engine()
    try:
        with pytest.raises(UnsupportedTMPOpError) as excinfo:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.set_project_issuetype_screen_scheme(
                    project_alias="growth",
                    scheme_alias="grw_itss",
                ),
            )
    finally:
        await engine.close()
    msg = str(excinfo.value)
    assert "team-managed" in msg
    assert f"{BASE}/jira/software/projects/GRW/settings/details" in msg


@pytest.mark.asyncio
async def test_set_project_field_configuration_scheme_refuses_tmp():
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["growth"] = ProjectMapping(
        id="p-1",
        style="team-managed",
        key="GRW",
    )
    state.field_configuration_schemes["grw_fcs"] = SimpleMapping(id="fcs-1")
    engine = _dc_engine()
    try:
        with pytest.raises(UnsupportedTMPOpError) as excinfo:
            await _run_in_ctx(
                engine,
                state,
                lambda: op.set_project_field_configuration_scheme(
                    project_alias="growth",
                    scheme_alias="grw_fcs",
                ),
            )
    finally:
        await engine.close()
    assert f"{BASE}/jira/software/projects/GRW/settings/details" in str(excinfo.value)


@pytest.mark.asyncio
@respx.mock
async def test_set_project_issuetype_screen_scheme_allowed_on_cmp():
    """Sanity check: CMP project doesn't trigger the TMP guard."""
    respx.put(f"{DC_ROOT}/issuetypescreenscheme/project").mock(
        return_value=httpx.Response(204),
    )
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["plat"] = ProjectMapping(id="p-1", key="PLAT")
    state.issuetype_screen_schemes["plat_itss"] = SimpleMapping(id="itss-1")
    engine = _dc_engine()
    try:
        await _run_in_ctx(
            engine,
            state,
            lambda: op.set_project_issuetype_screen_scheme(
                project_alias="plat",
                scheme_alias="plat_itss",
            ),
        )
    finally:
        await engine.close()


# ── Stamp captures style ─────────────────────────────────────────────
def test_stamp_classifies_next_gen_as_team_managed():
    from pensum import IssueType, Project

    class _Task(IssueType):
        __alias__ = "task"
        summary: str

    class _Growth(Project):
        __key__ = "GRW"
        __style__ = "team-managed"
        __issuetypes__ = [_Task]

    state = StateFile(env="dev", jira_url=BASE)
    snap = _empty_snapshot()
    snap.projects["GRW"] = ProjectSnapshot(
        id="p-1",
        key="GRW",
        name="Growth",
        style="next-gen",
    )
    stamp(state, snap)
    assert state.projects["GRW"].style == "team-managed"
    assert state.projects["GRW"].key == "GRW"


def test_stamp_classifies_classic_as_company_managed():
    from pensum import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __issuetypes__ = [_Bug]

    state = StateFile(env="dev", jira_url=BASE)
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="Platform",
        style="classic",
    )
    stamp(state, snap)
    assert state.projects["PLAT"].style == "company-managed"


def test_stamp_backfills_style_on_pre_m7_state():
    """A state file from before M7 records id only, defaulting style to CMP.
    If Jira actually has TMP, the next stamp should fix it."""
    from pensum import IssueType, Project

    class _Task(IssueType):
        __alias__ = "task"
        summary: str

    class _Growth(Project):
        __key__ = "GRW"
        __style__ = "team-managed"
        __issuetypes__ = [_Task]

    state = StateFile(env="dev", jira_url=BASE)
    state.projects["GRW"] = ProjectMapping(id="p-1")  # no style, no key
    snap = _empty_snapshot()
    snap.projects["GRW"] = ProjectSnapshot(
        id="p-1",
        key="GRW",
        name="Growth",
        style="next-gen",
    )
    stamp(state, snap)
    assert state.projects["GRW"].style == "team-managed"
    assert state.projects["GRW"].key == "GRW"


def test_stamp_skips_id_divergence_without_overwriting_style():
    from pensum import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __issuetypes__ = [_Bug]

    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(
        id="p-OLD",
        style="team-managed",
        key="PLAT",
    )
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-NEW",
        key="PLAT",
        name="Platform",
        style="classic",
    )
    report = stamp(state, snap)
    assert state.projects["PLAT"].id == "p-OLD"
    assert state.projects["PLAT"].style == "team-managed"
    assert any(entry[0] == "project" and entry[1] == "PLAT" for entry in report.skipped)


# ── Diff warns on style mismatch ─────────────────────────────────────
def test_diff_warns_on_style_mismatch():
    from pensum import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __style__ = "team-managed"  # schema says TMP …
        __issuetypes__ = [_Bug]

    desired = build_desired_snapshot()
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(
        id="p-1",
        style="company-managed",
        key="PLAT",  # … but state has CMP
    )
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="_Plat",
        style="classic",
    )
    result = diff(desired=desired, snapshot=snap, state=state, allow_delete=False)
    assert any("team-managed" in w and "company-managed" in w and "PLAT" in w for w in result.warnings)
    # Style alone produces no UpdateProject change — Jira has no REST conversion.
    updates = [c for c in result.changes if isinstance(c, UpdateProject)]
    assert updates == []


def test_diff_quiet_when_styles_match():
    from pensum import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    class _Plat(Project):
        __key__ = "PLAT"
        __issuetypes__ = [_Bug]

    desired = build_desired_snapshot()
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["PLAT"] = ProjectMapping(
        id="p-1",
        style="company-managed",
        key="PLAT",
    )
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="_Plat",
        style="classic",
    )
    result = diff(desired=desired, snapshot=snap, state=state, allow_delete=False)
    assert not any("style" in w for w in result.warnings)


# ── DesiredProject + Project metaclass ───────────────────────────────
def test_desired_project_reads_style():
    from pensum import IssueType, Project

    class _Task(IssueType):
        __alias__ = "task"
        summary: str

    class _Growth(Project):
        __key__ = "GRW"
        __style__ = "team-managed"
        __issuetypes__ = [_Task]

    desired = build_desired_snapshot()
    assert desired.projects["GRW"].style == "team-managed"


def test_project_metaclass_rejects_unknown_style():
    from pensum import IssueType, Project

    class _Bug(IssueType):
        __alias__ = "bug"
        summary: str

    with pytest.raises(ConfigurationError, match="invalid __style__"):

        class _Bad(Project):
            __key__ = "BAD"
            __style__ = "hybrid"
            __issuetypes__ = [_Bug]


def test_project_metaclass_rejects_tmp_with_screen_scheme():
    from pensum import IssueType, Project, Screen, ScreenScheme

    s = Screen(alias="s", name="S", fields=["Summary"])
    ss = ScreenScheme(alias="ss", name="SS", create=s, edit=s, view=s)

    class _Bug(IssueType):
        __alias__ = "bug"
        __screen_scheme__ = ss
        summary: str

    with pytest.raises(ConfigurationError, match="team-managed.*ScreenScheme"):

        class _Bad(Project):
            __key__ = "BAD"
            __style__ = "team-managed"
            __issuetypes__ = [_Bug]


def test_project_metaclass_rejects_tmp_with_field_configuration():
    from pensum import FieldConfiguration, IssueType, Project

    fc = FieldConfiguration(alias="fc", name="FC", required=["Summary"])

    class _Bug(IssueType):
        __alias__ = "bug"
        __field_configuration__ = fc
        summary: str

    with pytest.raises(ConfigurationError, match="team-managed.*FieldConfiguration"):

        class _Bad(Project):
            __key__ = "BAD"
            __style__ = "team-managed"
            __issuetypes__ = [_Bug]
