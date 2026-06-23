"""Stamp (brownfield): match schema declarations to existing Jira objects."""

import sys

import httpx
import pytest
import respx

from stint import StateFile
from stint.autogen.stamp import stamp
from stint.cli.main import main
from stint.fields import SelectField, TextField
from stint.registry import registry
from stint.state.snapshot import (
    CustomFieldSnapshot,
    ProjectSnapshot,
    ScreenSnapshot,
    ScreenTabSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.platform", None)


def _empty_snapshot() -> Snapshot:
    return Snapshot(
        server_info=ServerInfoSnapshot(
            deployment_type="Server",
            version="9",
            base_url="x",
        )
    )


# ── Match by name ────────────────────────────────────────────────────
def test_stamp_matches_custom_field_by_name():
    from stint.fields import CustomField

    CustomField(alias="bug_severity", name="Severity", type=TextField)
    state = StateFile(env="dev", jira_url="x")
    snap = _empty_snapshot()
    snap.custom_fields["customfield_10042"] = CustomFieldSnapshot(
        id="customfield_10042",
        name="Severity",
        type_id=TextField.jira_type_id,
    )
    report = stamp(state, snap)
    assert ("custom_field", "bug_severity", "customfield_10042") in report.matched
    assert state.custom_fields["bug_severity"].id == "customfield_10042"


def test_stamp_matches_select_field_with_options():
    from stint.fields import CustomField

    CustomField(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        options=["S1", "S2"],
    )
    state = StateFile(env="dev", jira_url="x")
    snap = _empty_snapshot()
    snap.custom_fields["customfield_10042"] = CustomFieldSnapshot(
        id="customfield_10042",
        name="Severity",
        type_id=SelectField.jira_type_id,
        options={"S1": "100", "S2": "101"},
    )
    stamp(state, snap)
    mapping = state.custom_fields["bug_severity"]
    assert mapping.id == "customfield_10042"
    assert mapping.options == {"S1": "100", "S2": "101"}


def test_stamp_matches_screen_and_populates_tab_ids():
    from stint.schema.screen import Screen

    Screen(alias="bug_screen", name="Bug Screen", fields=["Summary"])
    state = StateFile(env="dev", jira_url="x")
    snap = _empty_snapshot()
    snap.screens["scr-1"] = ScreenSnapshot(
        id="scr-1",
        name="Bug Screen",
        description="",
        tabs=(ScreenTabSnapshot(id="tab-7", name="Fields"),),
    )
    report = stamp(state, snap)
    assert ("screen", "bug_screen", "scr-1") in report.matched
    assert state.screens["bug_screen"].tab_ids == {"Fields": "tab-7"}


def test_stamp_matches_project_by_key():
    import examples.platform  # noqa: F401

    state = StateFile(env="dev", jira_url="x")
    snap = _empty_snapshot()
    snap.projects["PLAT"] = ProjectSnapshot(
        id="p-1",
        key="PLAT",
        name="Platform",
    )
    # Add the rest of the platform's expected objects so stamp doesn't choke;
    # but we only assert the project match here.
    report = stamp(state, snap)
    assert ("project", "PLAT", "p-1") in report.matched
    assert state.projects["PLAT"].id == "p-1"


# ── Unmatched ────────────────────────────────────────────────────────
def test_stamp_records_unmatched():
    from stint.fields import CustomField

    CustomField(alias="missing", name="Not In Jira", type=TextField)
    state = StateFile(env="dev", jira_url="x")
    snap = _empty_snapshot()
    report = stamp(state, snap)
    assert ("custom_field", "missing") in report.unmatched
    assert "missing" not in state.custom_fields


# ── Conflict ─────────────────────────────────────────────────────────
def test_stamp_skips_if_alias_already_mapped_to_different_id():
    from stint.fields import CustomField
    from stint.state.file import CustomFieldMapping

    CustomField(alias="bug_severity", name="Severity", type=TextField)
    state = StateFile(env="dev", jira_url="x")
    state.custom_fields["bug_severity"] = CustomFieldMapping(id="customfield_OLD")

    snap = _empty_snapshot()
    snap.custom_fields["customfield_NEW"] = CustomFieldSnapshot(
        id="customfield_NEW",
        name="Severity",
        type_id=TextField.jira_type_id,
    )
    report = stamp(state, snap)
    # No match recorded (state preserved); skipped emitted.
    assert state.custom_fields["bug_severity"].id == "customfield_OLD"
    assert any(kind == "custom_field" and alias == "bug_severity" for kind, alias, _ in report.skipped)


# ── Derived schemes (ITSS/FCS by synthesized name) ──────────────────
def test_stamp_matches_derived_itss_by_synthesized_name():
    import examples.platform  # noqa: F401

    state = StateFile(env="dev", jira_url="x")
    snap = _empty_snapshot()
    # The synthesized name comes from desired.py:
    # f"{project_key} Issue Type Screen Scheme"
    from stint.state.snapshot import IssueTypeScreenSchemeSnapshot

    snap.issuetype_screen_schemes["itss-1"] = IssueTypeScreenSchemeSnapshot(
        id="itss-1",
        name="PLAT Issue Type Screen Scheme",
    )
    report = stamp(state, snap)
    assert ("issuetype_screen_scheme", "PLAT_itss", "itss-1") in report.matched


# ── CLI smoke ────────────────────────────────────────────────────────
BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


def _paginated(values):
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


@respx.mock
def test_cli_stamp_smoke(tmp_path, monkeypatch, capsys):
    """End-to-end: stamp loads schema, hits /reflect endpoints, writes state."""
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "1001.0.0",
                "deploymentType": "Cloud",
            },
        )
    )
    respx.get(f"{CLOUD_ROOT}/field").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "customfield_10042", "name": "Severity", "schema": {"custom": SelectField.jira_type_id}},
                {"id": "customfield_10043", "name": "Root Cause", "schema": {"custom": TextField.jira_type_id}},
            ],
        )
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context").mock(
        return_value=httpx.Response(200, json=_paginated([{"id": "ctx-1"}]))
    )
    respx.get(f"{CLOUD_ROOT}/field/customfield_10042/context/ctx-1/option").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "100", "value": "S1"},
                    {"id": "101", "value": "S2"},
                    {"id": "102", "value": "S3"},
                    {"id": "103", "value": "S4"},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetype").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "10010", "name": "Bug", "description": "", "subtask": False},
            ],
        )
    )
    respx.get(f"{CLOUD_ROOT}/project/search").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "p-1", "key": "PLAT", "name": "Platform"},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/screens").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "scr-1", "name": "Bug Create Screen"},
                    {"id": "scr-2", "name": "Bug Edit Screen"},
                    {"id": "scr-3", "name": "Bug View Screen"},
                ]
            ),
        )
    )
    for sid in ("scr-1", "scr-2", "scr-3"):
        respx.get(f"{CLOUD_ROOT}/screens/{sid}/tabs").mock(
            return_value=httpx.Response(200, json=[{"id": f"{sid}-tab", "name": "Fields"}])
        )
        respx.get(f"{CLOUD_ROOT}/screens/{sid}/tabs/{sid}-tab/fields").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{CLOUD_ROOT}/screenscheme").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {
                        "id": "ss-1",
                        "name": "Bug Screen Scheme",
                        "screens": {"default": "scr-3", "create": "scr-1", "edit": "scr-2"},
                    },
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/issuetypescheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/mapping").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/issuetypescheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/issuetypescreenscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/issuetypescreenscheme/project").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/fieldconfiguration").mock(
        return_value=httpx.Response(
            200,
            json=_paginated(
                [
                    {"id": "fc-1", "name": "Bug Field Configuration"},
                ]
            ),
        )
    )
    respx.get(f"{CLOUD_ROOT}/fieldconfiguration/fc-1/fields").mock(
        return_value=httpx.Response(200, json=_paginated([]))
    )
    respx.get(f"{CLOUD_ROOT}/fieldconfigurationscheme").mock(return_value=httpx.Response(200, json=_paginated([])))
    respx.get(f"{CLOUD_ROOT}/fieldconfigurationscheme/project").mock(
        return_value=httpx.Response(200, json=_paginated([]))
    )

    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "stamp",
            "--schema",
            "examples.platform",
            "--state",
            str(state_path),
            "--env",
            "prod",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "matched=" in out
    on_disk = StateFile.load(state_path)
    assert on_disk.custom_fields["bug_severity"].id == "customfield_10042"
    assert on_disk.custom_fields["bug_severity"].options == {
        "S1": "100",
        "S2": "101",
        "S3": "102",
        "S4": "103",
    }
    assert on_disk.issuetypes["bug"].id == "10010"
    assert on_disk.projects["PLAT"].id == "p-1"
    assert on_disk.screens["bug_view"].id == "scr-3"
