"""`stint apply` CLI: schema/project validation, plan printing, the
interactive confirm/--auto-approve/--dry-run gates, and state-file
persistence. TmpDialect's individual operations and apply_tmp_plan's
dispatch are already covered by test_m9_tmp_dialect.py / test_m9_tmp_reconcile.py;
this file is about the CLI wiring layer on top of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from stint.cli.main import main
from stint.registry import registry
from stint.state.file import StateFile

BASE = "https://cumulusec.atlassian.net"
API_ROOT = f"{BASE}/rest/api/3"
GATEWAY_URL = f"{BASE}/gateway/api/graphql"
GIRA_URL = f"{BASE}/rest/gira/1/"
SIMPLIFIED_URL = f"{BASE}/rest/internal/simplified/1.0"
ISSUE_LAYOUTS_URL = f"{BASE}/rest/internal/1.0/issueLayouts"
TENANT_INFO_URL = f"{BASE}/_edge/tenant_info"
ISSUETYPE_PROJECT_URL = f"{API_ROOT}/issuetype/project"

_TMP_SCHEMA = """
from typing import Annotated

from stint import CustomField, IssueType, Project
from stint.fields import TextField

root_cause_cf = CustomField(alias="root_cause", name="Root Cause", type=TextField)


class Bug(IssueType):
    __alias__ = "bug"
    __title__ = "Bug"
    __description__ = "A bug"

    root_cause: Annotated[str, root_cause_cf]


class Vuln(Project):
    __key__ = "VM"
    __style__ = "team-managed"
    __issuetypes__ = [Bug]


class Widgets(Project):
    __key__ = "CMP"
    __issuetypes__ = [Bug]
"""


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    yield
    registry.reset()


def _write_schema(tmp_path: Path) -> str:
    p = tmp_path / "schema.py"
    p.write_text(_TMP_SCHEMA)
    return str(p)


def _project_by_key_response(project_id: int, uuid: str, key: str, name: str, style: str) -> dict:
    return {
        "data": {
            "jira_projectByIdOrKey": {
                "projectId": project_id,
                "key": key,
                "uuid": uuid,
                "name": name,
                "projectType": "software",
                "projectStyle": style,
            }
        }
    }


def _enumeration_response(edges: list[dict]) -> dict:
    return {
        "data": {
            "jira": {
                "jiraProjectByKey": {
                    "projectWithVisibleIssueTypeIds": {
                        "fieldAssociationWithIssueTypes": {"edges": edges, "pageInfo": {"hasNextPage": False}}
                    }
                }
            }
        }
    }


def _field_create_response(field_id: str, name: str) -> dict:
    return {
        "data": {
            "jira": {
                "createCustomFieldInProjectAndAddToAllIssueTypes": {
                    "success": True,
                    "fieldAssociationWithIssueTypes": {
                        "field": {"fieldId": field_id, "name": name, "description": "", "scope": "PROJECT"},
                        "fieldOptions": {"edges": []},
                    },
                }
            }
        }
    }


def _gateway_dispatch():
    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        op = body.get("operationName")
        if op == "StintTmpResolveProject":
            return httpx.Response(
                200,
                json=_project_by_key_response(
                    10001, "proj-uuid-1", "VM", "Vulnerability Management", "TEAM_MANAGED_PROJECT"
                ),
            )
        if op == "StintTmpFieldEnumeration":
            return httpx.Response(200, json=_enumeration_response([]))
        if op == "StintTmpFieldCreate":
            return httpx.Response(200, json=_field_create_response("customfield_1", "Root Cause"))
        raise AssertionError(f"no mocked response for operationName {op!r}")

    return _respond


def _stub_reflect_only(mock: respx.MockRouter) -> None:
    """Endpoints reflect() needs. Deliberately does NOT mock field-create,
    worktype-create, or layout read/write -- if apply proceeds to write
    anyway (e.g. a --dry-run or cancel bug), respx raises instead of
    silently succeeding."""
    mock.get(TENANT_INFO_URL).mock(return_value=httpx.Response(200, json={"cloudId": "cloud-1"}))
    mock.get(f"{API_ROOT}/serverInfo").mock(
        return_value=httpx.Response(200, json={"deploymentType": "Cloud", "version": "1001.0.0", "baseUrl": BASE})
    )
    mock.post(GATEWAY_URL).mock(side_effect=_gateway_dispatch())
    mock.get(ISSUETYPE_PROJECT_URL).mock(return_value=httpx.Response(200, json=[]))


def _stub_full_apply(mock: respx.MockRouter) -> None:
    _stub_reflect_only(mock)
    mock.post(f"{SIMPLIFIED_URL}/project/10001/settings/issuetype").mock(
        return_value=httpx.Response(201, json={"id": "10007", "name": "Bug", "avatarId": 10321, "hierarchyLevel": 0})
    )
    mock.post(GIRA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueLayoutConfiguration": {
                        "__typename": "JiraIssueLayoutConfigurationResult",
                        "issueLayoutResult": {
                            "id": "layout-x",
                            "name": "VM-Bug",
                            "usageInfo": {
                                "edges": [
                                    {
                                        "currentProject": True,
                                        "node": {
                                            "layoutOwners": [
                                                {
                                                    "__typename": "JiraIssueLayoutIssueTypeOwner",
                                                    "id": "10007",
                                                    "name": "Bug",
                                                    "description": "A bug",
                                                    "avatarId": "10321",
                                                    "iconUrl": "x",
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                            "containers": [],
                        },
                        "metadata": {"configuration": {"items": {"nodes": []}}},
                    }
                }
            },
        )
    )
    mock.put(f"{ISSUE_LAYOUTS_URL}/layout-x").mock(
        return_value=httpx.Response(
            200,
            json={
                "owners": [
                    {
                        "type": "ISSUE_TYPE",
                        "data": {"id": 10007, "name": "Bug", "description": "A bug", "avatarId": 10321, "iconUrl": "x"},
                    }
                ],
                "issueLayoutConfig": {"items": []},
            },
        )
    )


def _base_args(schema: str, state_path: Path, *, extra: list[str] | None = None) -> list[str]:
    args = [
        "apply",
        "--schema",
        schema,
        "--project-key",
        "VM",
        "--state",
        str(state_path),
        "--env",
        "prod",
        "--url",
        f"jira_cloud_tmp+{BASE}",
        "--auth",
        "api-token",
    ]
    return args + (extra or [])


@respx.mock
def test_cli_apply_dry_run_shows_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    _stub_reflect_only(respx.mock)
    schema = _write_schema(tmp_path)
    state_path = tmp_path / "state.yaml"

    rc = main(_base_args(schema, state_path, extra=["--dry-run"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Plan: 2 to add, 1 to change, 0 to destroy." in out
    assert not state_path.exists()


@respx.mock
def test_cli_apply_cancelled_when_not_confirmed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    monkeypatch.setattr("builtins.input", lambda _: "no")
    _stub_reflect_only(respx.mock)
    schema = _write_schema(tmp_path)
    state_path = tmp_path / "state.yaml"

    rc = main(_base_args(schema, state_path))
    assert rc == 1
    assert "Apply cancelled." in capsys.readouterr().out
    assert not state_path.exists()


@respx.mock
def test_cli_apply_auto_approve_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    _stub_full_apply(respx.mock)
    schema = _write_schema(tmp_path)
    state_path = tmp_path / "state.yaml"

    rc = main(_base_args(schema, state_path, extra=["--auto-approve"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Plan: 2 to add, 1 to change, 0 to destroy." in out
    assert "Apply complete! Resources: 2 added, 1 changed, 0 destroyed." in out

    reloaded = StateFile.load(state_path)
    tmp_state = reloaded.tmp_projects["VM"]
    assert tmp_state.fields["root_cause"] == "customfield_1"
    assert tmp_state.worktypes["bug"] == "10007"
    assert tmp_state.layout_ids["bug"] == "layout-x"
    assert reloaded.projects["VM"].style == "team-managed"
    assert reloaded.projects["VM"].id == "10001"


def test_cli_apply_rejects_non_team_managed_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    schema = _write_schema(tmp_path)
    state_path = tmp_path / "state.yaml"

    args = _base_args(schema, state_path)
    args[args.index("VM")] = "CMP"
    rc = main(args)
    assert rc == 1
    assert "team-managed" in capsys.readouterr().err


def test_cli_apply_rejects_unknown_project_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    schema = _write_schema(tmp_path)
    state_path = tmp_path / "state.yaml"

    args = _base_args(schema, state_path)
    args[args.index("VM")] = "MISSING"
    rc = main(args)
    assert rc == 1
    assert "no Project with __key__" in capsys.readouterr().err


def test_cli_apply_rejects_jira_cloud_dialect(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    schema = _write_schema(tmp_path)
    state_path = tmp_path / "state.yaml"

    args = [
        "apply",
        "--schema",
        schema,
        "--project-key",
        "VM",
        "--state",
        str(state_path),
        "--env",
        "prod",
        "--url",
        f"jira_cloud+{BASE}",
        "--auth",
        "pat",
    ]
    rc = main(args)
    assert rc == 1
    assert "jira_cloud_tmp" in capsys.readouterr().err
