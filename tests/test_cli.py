"""`stint reflect` CLI: argument parsing, auth wiring, output format."""

from typing import Any

import httpx
import pytest
import respx
import yaml

from stint.cli.main import main

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


def _paginated(values: list[Any]) -> dict[str, Any]:
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def _stub_empty(mock: respx.MockRouter) -> None:
    mock.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "9.12.4",
                "deploymentType": "Server",
            },
        )
    )
    for path in (
        f"{CLOUD_ROOT}/field",
        f"{CLOUD_ROOT}/issuetype",
    ):
        mock.get(path).mock(return_value=httpx.Response(200, json=[]))
    for path in (
        f"{CLOUD_ROOT}/field/search",
        f"{CLOUD_ROOT}/project/search",
        f"{CLOUD_ROOT}/screens",
        f"{CLOUD_ROOT}/screenscheme",
        f"{CLOUD_ROOT}/issuetypescheme",
        f"{CLOUD_ROOT}/issuetypescheme/mapping",
        f"{CLOUD_ROOT}/issuetypescheme/project",
        f"{CLOUD_ROOT}/issuetypescreenscheme",
        f"{CLOUD_ROOT}/issuetypescreenscheme/project",
        f"{CLOUD_ROOT}/fieldconfiguration",
        f"{CLOUD_ROOT}/fieldconfigurationscheme",
        f"{CLOUD_ROOT}/fieldconfigurationscheme/project",
    ):
        mock.get(path).mock(return_value=httpx.Response(200, json=_paginated([])))


@respx.mock
def test_reflect_yaml_output(monkeypatch, capsys):
    _stub_empty(respx.mock)
    monkeypatch.setenv("STINT_TOKEN", "test-pat")
    rc = main(
        [
            "reflect",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    parsed = yaml.safe_load(out)
    assert parsed["server_info"]["deployment_type"] == "Server"
    assert parsed["server_info"]["version"] == "9.12.4"
    assert parsed["custom_fields"] == {}
    assert parsed["issuetypes"] == {}


@respx.mock
def test_reflect_json_output(monkeypatch, capsys):
    _stub_empty(respx.mock)
    monkeypatch.setenv("STINT_TOKEN", "test-pat")
    rc = main(
        [
            "reflect",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    import json as _json

    parsed = _json.loads(capsys.readouterr().out)
    assert parsed["server_info"]["deployment_type"] == "Server"


@respx.mock
def test_domain_error_prints_clean_message_not_traceback(monkeypatch, capsys):
    """A backend error surfaces as one 'ERROR:' line with exit 1, not a Python
    traceback dumped at the user."""
    respx.get(f"{CLOUD_ROOT}/serverInfo").mock(return_value=httpx.Response(500, text="kaboom"))
    monkeypatch.setenv("STINT_TOKEN", "test-pat")
    rc = main(["reflect", "--url", f"jira_cloud+{BASE}", "--auth", "pat"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("ERROR: ")
    assert "Traceback" not in captured.err and "Traceback" not in captured.out


def test_reflect_missing_pat_env_exits(monkeypatch):
    monkeypatch.delenv("STINT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reflect",
                "--url",
                f"jira_cloud+{BASE}",
                "--auth",
                "pat",
            ]
        )
    assert "STINT_TOKEN" in str(exc.value)


def test_reflect_missing_basic_auth_exits(monkeypatch):
    monkeypatch.delenv("STINT_TOKEN", raising=False)
    monkeypatch.delenv("STINT_USER", raising=False)
    with pytest.raises(SystemExit):
        main(
            [
                "reflect",
                "--url",
                f"jira_cloud+{BASE}",
                "--auth",
                "basic",
            ]
        )


@respx.mock
def test_reflect_dialect_kwarg_without_url_prefix(monkeypatch, capsys):
    _stub_empty(respx.mock)
    monkeypatch.setenv("STINT_TOKEN", "test-pat")
    rc = main(
        [
            "reflect",
            "--url",
            BASE,
            "--dialect",
            "jira_cloud",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Server" in out


def test_reflect_requires_url_argument():
    with pytest.raises(SystemExit):
        main(["reflect", "--auth", "pat"])


# ── stint current / history ──────────────────────────────────────────
def test_current_with_no_state_file(tmp_path, capsys):
    rc = main(["current", "--state", str(tmp_path / "missing.yaml")])
    assert rc == 0
    assert "no state file" in capsys.readouterr().out


def test_current_reads_revision_from_state_file(tmp_path, capsys):
    from stint import StateFile

    sf = StateFile(env="dev", jira_url="https://x", revision="abc12345")
    p = tmp_path / "state.yaml"
    sf.save(p)
    rc = main(["current", "--state", str(p)])
    assert rc == 0
    assert "abc12345" in capsys.readouterr().out


def test_history_lists_migrations_in_order(capsys):
    from pathlib import Path

    fixtures = str(Path(__file__).parent / "fixtures" / "migrations")
    rc = main(["history", "--migrations-dir", fixtures])
    assert rc == 0
    out = capsys.readouterr().out
    # Both revisions listed in chain order
    assert out.index("abc123de") < out.index("def789gh")
    assert "initial bug severity" in out
    assert "add root cause" in out
