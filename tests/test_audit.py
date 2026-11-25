"""Screen-locked ("control") fields: the cosmetic-lock check rule and the
out-of-band write audit.

Covers the shared control-field derivation, the `engine.check` warning for
the restricted-Edit-screen + broad EDIT_ISSUES combination (JRACLOUD-98054),
and `engine.audit_control_fields` scanning changelog entries for writes by
identities outside the allow-list.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from stint import (
    CustomField,
    IssueType,
    PATAuth,
    Project,
    Screen,
    ScreenScheme,
    SelectField,
    StateFile,
    create_engine,
    registry,
)
from stint.audit import audit_control_fields, declared_control_fields
from stint.check import check_project
from stint.state.file import CustomFieldMapping, ProjectMapping
from stint.state.snapshot import (
    PermissionGrantSnapshot,
    PermissionSchemeSnapshot,
    ProjectSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"

impact_field = CustomField(
    alias="ticket_impact",
    name="Impact",
    type=SelectField,
    options=["Low", "High"],
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    yield
    registry.reset()


def _schema() -> type[Project]:
    view_screen = Screen(alias="t_view", name="V", fields=["Summary", impact_field, "Labels"])
    edit_screen = Screen(alias="t_edit", name="E", fields=["Summary"])  # Impact locked
    scheme = ScreenScheme(alias="t_screens", name="S", create=view_screen, edit=edit_screen, view=view_screen)

    class Ticket(IssueType):
        __alias__ = "ticket"
        __screen_scheme__ = scheme

    class Tracker(Project):
        __key__ = "TRK"
        __issuetypes__ = [Ticket]

    return Tracker


def _state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.projects["TRK"] = ProjectMapping(id="10010", style="company-managed", key="TRK")
    state.custom_fields["ticket_impact"] = CustomFieldMapping(id="customfield_10042")
    return state


def _snapshot(*, edit_issues_grants: tuple[PermissionGrantSnapshot, ...] = ()) -> Snapshot:
    snap = Snapshot(server_info=ServerInfoSnapshot(deployment_type="Cloud", version="1", base_url=BASE))
    snap.projects["TRK"] = ProjectSnapshot(
        id="10010",
        key="TRK",
        name="TRK",
        permission_scheme_id="20000",
    )
    snap.permission_schemes["20000"] = PermissionSchemeSnapshot(
        id="20000",
        name="Default",
        grants=edit_issues_grants,
    )
    snap.project_roles = {"Administrators": "10360"}
    return snap


# ── Control-field derivation ─────────────────────────────────────────
def test_declared_control_fields():
    control = declared_control_fields(_schema())
    assert control == {"ticket": ("ticket_impact", "Labels")}


def test_immutable_system_fields_are_never_control_fields():
    """Created/Updated cannot be written via REST by anyone, so they are not
    cosmetic-lock candidates (live-verified false positive on a tenant whose
    only create/view-minus-edit fields were these two)."""
    view_screen = Screen(alias="v", name="V", fields=["Summary", "Created", "Updated"])
    scheme = ScreenScheme(
        alias="s", name="S", create=view_screen, edit=Screen(alias="e", name="E", fields=["Summary"]), view=view_screen
    )

    class T(IssueType):
        __alias__ = "t"
        __screen_scheme__ = scheme

    class P(Project):
        __key__ = "P3"
        __issuetypes__ = [T]

    assert declared_control_fields(P) == {}


def test_declared_control_fields_empty_when_edit_covers_all():
    view_screen = Screen(alias="v", name="V", fields=["Summary"])
    scheme = ScreenScheme(alias="s", name="S", create=view_screen, edit=view_screen, view=view_screen)

    class T(IssueType):
        __alias__ = "t"
        __screen_scheme__ = scheme

    class P(Project):
        __key__ = "P1"
        __issuetypes__ = [T]

    assert declared_control_fields(P) == {}


# ── engine.check: the cosmetic-lock warning ──────────────────────────
def _run_check(snapshot: Snapshot, state: StateFile, project_cls: type[Project]) -> Any:
    engine = create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))

    class _FakeDialect:
        async def reflect(self) -> Snapshot:
            return snapshot

        async def get_createmeta(self, project_key: str) -> dict[str, Any]:
            return {"projects": [{"issuetypes": [{"id": "1", "fields": {"summary": {}}}]}]}

    engine.dialect = _FakeDialect()  # ty: ignore[invalid-assignment]
    import asyncio

    return asyncio.run(check_project(engine, project_cls, state))


def test_check_warns_when_edit_screen_lock_is_cosmetic():
    snapshot = _snapshot(
        edit_issues_grants=(
            PermissionGrantSnapshot(
                id="g1", permission="EDIT_ISSUES", holder_type="projectRole", holder_parameter="10360"
            ),
            PermissionGrantSnapshot(
                id="g2", permission="BROWSE_PROJECTS", holder_type="group", holder_parameter="all-staff"
            ),
        )
    )
    report = _run_check(snapshot, _state(), _schema())
    matches = [w for w in report.warnings if "JRACLOUD-98054" in w]
    assert len(matches) == 1
    assert "ticket_impact" in matches[0] and "Labels" in matches[0]
    # The projectRole holder renders as its role name, not the raw id.
    assert "project role 'Administrators'" in matches[0]


def test_check_silent_when_edit_issues_is_narrow():
    snapshot = _snapshot(edit_issues_grants=())
    report = _run_check(snapshot, _state(), _schema())
    assert not [w for w in report.warnings if "JRACLOUD-98054" in w]


def test_check_silent_without_control_fields():
    snapshot = _snapshot(
        edit_issues_grants=(
            PermissionGrantSnapshot(
                id="g1", permission="EDIT_ISSUES", holder_type="group", holder_parameter="all-staff"
            ),
        )
    )
    view_screen = Screen(alias="v", name="V", fields=["Summary"])
    scheme = ScreenScheme(alias="s", name="S", create=view_screen, edit=view_screen, view=view_screen)

    class T(IssueType):
        __alias__ = "t"
        __screen_scheme__ = scheme

    class P(Project):
        __key__ = "TRK"
        __issuetypes__ = [T]

    report = _run_check(snapshot, _state(), P)
    assert not [w for w in report.warnings if "JRACLOUD-98054" in w]


# ── engine.audit_control_fields ──────────────────────────────────────
def _engine_with_mock(mock: respx.MockRouter, *, changelog: list[dict[str, Any]]):
    mock.post(f"{CLOUD_ROOT}/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "TRK-1"}], "nextPageToken": None})
    )
    mock.get(f"{CLOUD_ROOT}/user/search").mock(
        return_value=httpx.Response(200, json=[{"accountId": "acct-svc", "emailAddress": "svc@example.com"}])
    )
    mock.get(f"{CLOUD_ROOT}/issue/TRK-1/changelog").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": changelog,
                "startAt": 0,
                "isLast": True,
                "maxResults": len(changelog),
            },
        )
    )
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


def _entry(*, account: str, field_id: str, field_name: str, to: str, created: str = "2026-11-20T09:00:00.000+0000"):
    return {
        "id": "e1",
        "created": created,
        "author": {"accountId": account, "displayName": account},
        "items": [
            {
                "field": field_name,
                "fieldId": field_id,
                "from": "Low",
                "fromString": "Low",
                "to": to,
                "toString": to,
            }
        ],
    }


@pytest.mark.asyncio
@respx.mock
async def test_audit_reports_out_of_band_writes():
    mock = respx.mock
    engine = _engine_with_mock(
        mock,
        changelog=[
            _entry(account="acct-svc", field_id="customfield_10042", field_name="Impact", to="High"),
            _entry(account="acct-intruder", field_id="customfield_10042", field_name="Impact", to="Low"),
        ],
    )
    state = _state()
    async with engine:
        report = await audit_control_fields(
            engine,
            _schema(),
            state,
            since="2026/11/01 00:00",
            allowed_writers=["svc@example.com"],
        )
    assert report.control_fields == ("Labels", "ticket_impact")
    assert len(report.writes) == 1
    write = report.writes[0]
    assert write.issue_key == "TRK-1"
    assert write.field == "ticket_impact"  # alias, not the raw id
    assert write.author == "acct-intruder"
    assert write.from_value == "Low" and write.to_value == "Low"
    with pytest.raises(Exception, match="out-of-band writes"):
        report.raise_for_writes()


@pytest.mark.asyncio
@respx.mock
async def test_audit_allows_service_writer_and_filters_old_entries():
    mock = respx.mock
    engine = _engine_with_mock(
        mock,
        changelog=[
            _entry(account="acct-svc", field_id="customfield_10042", field_name="Impact", to="High"),
            _entry(
                account="acct-old",
                field_id="customfield_10042",
                field_name="Impact",
                to="Low",
                created="2026-10-01T09:00:00.000+0000",
            ),
        ],
    )
    state = _state()
    from datetime import datetime

    async with engine:
        report = await audit_control_fields(
            engine,
            _schema(),
            state,
            since=datetime(2026, 11, 1),
            allowed_writers=["acct-svc"],
        )
    assert report.writes == []


@pytest.mark.asyncio
@respx.mock
async def test_audit_skips_non_control_fields():
    mock = respx.mock
    engine = _engine_with_mock(
        mock,
        changelog=[
            _entry(account="acct-who", field_id="customfield_99999", field_name="Notes", to="hi"),
        ],
    )
    state = _state()
    async with engine:
        report = await audit_control_fields(
            engine,
            _schema(),
            state,
            since="2026/11/01 00:00",
        )
    assert report.writes == []


@pytest.mark.asyncio
@respx.mock
async def test_audit_no_control_fields_is_a_noop():
    mock = respx.mock
    view_screen = Screen(alias="v", name="V", fields=["Summary"])
    scheme = ScreenScheme(alias="s", name="S", create=view_screen, edit=view_screen, view=view_screen)

    class T(IssueType):
        __alias__ = "t"
        __screen_scheme__ = scheme

    class P(Project):
        __key__ = "P2"
        __issuetypes__ = [T]

    engine = _engine_with_mock(mock, changelog=[])
    state = _state()
    async with engine:
        report = await audit_control_fields(engine, P, state, since="2026/11/01 00:00")
    assert report.control_fields == ()
    assert report.clean
    # No search was issued for a project without control fields.
    assert not [c for c in mock.calls if "/search/jql" in str(c.request.url)]
