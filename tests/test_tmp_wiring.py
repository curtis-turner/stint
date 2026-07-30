"""Core wiring for the TMP dialect: dialect selection (create_tmp_engine /
TmpEngine, separate from Engine/create_engine since TmpDialect does not
satisfy BaseDialect), the `stint reflect --dialect jira_cloud_tmp
--project-key` CLI path, and TmpState persistence via
StateFile.tmp_projects.

Isolation is also asserted here: `import stint` must never load
stint.dialects.jira.tmp (see engine.py's module docstring) -- only calling
create_tmp_engine does.
"""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest
import respx
import yaml

from stint.cli.main import main
from stint.client.auth import PATAuth
from stint.exceptions import ConfigurationError
from stint.state.file import StateFile, TmpProjectState

BASE = "https://cumulusec.atlassian.net"
API_ROOT = f"{BASE}/rest/api/3"
GATEWAY_URL = f"{BASE}/gateway/api/graphql"
GIRA_URL = f"{BASE}/rest/gira/1/"
TENANT_INFO_URL = f"{BASE}/_edge/tenant_info"
ISSUETYPE_PROJECT_URL = f"{API_ROOT}/issuetype/project"


# ── Isolation: import stint must not import the TMP package ──────────────
def test_import_stint_does_not_load_tmp_package():
    """Run in a fresh subprocess: this test file's own other tests import
    stint.dialects.jira.tmp for their own purposes, which would otherwise
    contaminate sys.modules before this assertion ever runs."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys, stint; print('dialects.jira.tmp' in ' '.join(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


# ── create_engine / create_tmp_engine selection ───────────────────────────
def test_create_engine_rejects_jira_cloud_tmp():
    from stint.engine import create_engine

    with pytest.raises(ConfigurationError, match="create_tmp_engine"):
        create_engine("https://jira.example.com", auth=PATAuth("tok"), dialect="jira_cloud_tmp")


def test_create_tmp_engine_rejects_jira_cloud():
    from stint.engine import create_tmp_engine

    with pytest.raises(ConfigurationError, match="jira_cloud_tmp"):
        create_tmp_engine("https://jira.example.com", auth=PATAuth("tok"), dialect="jira_cloud")


def test_create_tmp_engine_with_url_prefix():
    from stint.dialects.jira.tmp import TmpDialect
    from stint.engine import TmpEngine, create_tmp_engine

    with pytest.warns(UserWarning, match="experimental"):
        eng = create_tmp_engine("jira_cloud_tmp+https://jira.example.com", auth=PATAuth("tok"))
    assert isinstance(eng, TmpEngine)
    assert isinstance(eng.dialect, TmpDialect)
    assert eng.base_url == "https://jira.example.com"


def test_create_tmp_engine_with_kwarg_dialect():
    from stint.engine import create_tmp_engine

    with pytest.warns(UserWarning, match="experimental"):
        eng = create_tmp_engine("https://jira.example.com", auth=PATAuth("tok"), dialect="jira_cloud_tmp")
    assert eng.base_url == "https://jira.example.com"


def test_resolve_dialect_name_recognizes_both_dialects():
    from stint.engine import resolve_dialect_name

    assert resolve_dialect_name("jira_cloud+https://x", None) == ("https://x", "jira_cloud")
    assert resolve_dialect_name("jira_cloud_tmp+https://x", None) == ("https://x", "jira_cloud_tmp")


def test_resolve_dialect_name_unknown_raises():
    from stint.engine import resolve_dialect_name

    with pytest.raises(ConfigurationError, match="Unknown dialect"):
        resolve_dialect_name("https://x", "oracle_form_builder")


# ── CLI: stint reflect --dialect jira_cloud_tmp --project-key ────────────
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


def _gateway_dispatch(responses: dict[str, dict]):
    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        op = body.get("operationName")
        return httpx.Response(200, json=responses[op])

    return _respond


def _stub_tmp_reflect(mock: respx.MockRouter) -> None:
    mock.get(TENANT_INFO_URL).mock(return_value=httpx.Response(200, json={"cloudId": "cloud-1"}))
    mock.get(f"{API_ROOT}/serverInfo").mock(
        return_value=httpx.Response(200, json={"deploymentType": "Cloud", "version": "1001.0.0", "baseUrl": BASE})
    )
    mock.post(GATEWAY_URL).mock(
        side_effect=_gateway_dispatch(
            {
                "StintTmpResolveProject": _project_by_key_response(
                    10001, "proj-uuid-1", "VM", "Vulnerability Management", "TEAM_MANAGED_PROJECT"
                ),
                "StintTmpFieldEnumeration": _enumeration_response([]),
            }
        )
    )
    mock.get(ISSUETYPE_PROJECT_URL).mock(return_value=httpx.Response(200, json=[]))


@respx.mock
def test_cli_reflect_jira_cloud_tmp_end_to_end(monkeypatch, capsys):
    _stub_tmp_reflect(respx.mock)
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    rc = main(
        [
            "reflect",
            "--url",
            f"jira_cloud_tmp+{BASE}",
            "--auth",
            "api-token",
            "--project-key",
            "VM",
        ]
    )
    assert rc == 0
    parsed = yaml.safe_load(capsys.readouterr().out)
    assert parsed["snapshot"]["server_info"]["version"] == "1001.0.0"
    assert parsed["snapshot"]["projects"]["VM"]["style"] == "next-gen"


def test_cli_reflect_jira_cloud_tmp_requires_project_key(monkeypatch):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    monkeypatch.setenv("STINT_USER", "you@example.com")
    with pytest.raises(SystemExit, match="--project-key"):
        main(["reflect", "--url", f"jira_cloud_tmp+{BASE}", "--auth", "api-token"])


@respx.mock
def test_cli_reflect_jira_cloud_rejects_project_key(monkeypatch):
    monkeypatch.setenv("STINT_TOKEN", "test-token")
    with pytest.raises(SystemExit, match="--project-key"):
        main(
            [
                "reflect",
                "--url",
                f"jira_cloud+{BASE}",
                "--auth",
                "pat",
                "--project-key",
                "VM",
            ]
        )


# ── StateFile.tmp_projects persistence ────────────────────────────────────
def test_tmp_project_state_round_trips_through_state_file_yaml():
    state = StateFile(env="prod", jira_url=BASE)
    state.tmp_projects["vm"] = TmpProjectState(
        fields={"root_cause": "customfield_10179"},
        worktypes={"task": "10007"},
        layout_ids={"task": "c6b31d04-d184-48ef-877e-a11908f07576"},
    )
    reloaded = StateFile.from_yaml(state.to_yaml())
    assert reloaded.tmp_projects["vm"] == state.tmp_projects["vm"]


def test_tmp_project_state_omitted_when_empty():
    state = StateFile(env="prod", jira_url=BASE)
    assert "tmp_projects" not in yaml.safe_load(state.to_yaml())["mappings"]


def test_tmp_state_round_trips_through_tmp_project_state():
    from stint.dialects.jira.tmp.state import TmpState, from_project_state, to_project_state

    original = TmpState(
        fields={"root_cause": "customfield_10179"},
        worktypes={"task": "10007"},
        layout_ids={"task": "c6b31d04-d184-48ef-877e-a11908f07576"},
    )
    mapping = to_project_state(original)
    assert isinstance(mapping, TmpProjectState)
    restored = from_project_state(mapping)
    assert restored == original
