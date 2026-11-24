"""End-to-end test of `stint revision --autogenerate` CLI."""

import sys

import httpx
import pytest
import respx

from stint import StateFile, load_migrations
from stint.cli.main import main
from stint.registry import registry

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)


def _paginated(values):
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def _stub_empty_jira(mock, *, roles: bool = True):
    """All admin endpoints return empty — Jira has nothing.

    ``roles=False`` skips the role-actor / permission-scheme stubs so a test
    can register real payloads for them (respx matches the first-registered
    route, so the shared empty regexes would otherwise shadow overrides)."""
    mock.get(f"{CLOUD_ROOT}/serverInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "baseUrl": BASE,
                "version": "9",
                "deploymentType": "Cloud",
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/field").mock(return_value=httpx.Response(200, json=[]))
    mock.get(f"{CLOUD_ROOT}/field/search").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuetype").mock(return_value=httpx.Response(200, json=[]))
    for path in (
        "/project/search",
        "/screens",
        "/screenscheme",
        "/issuetypescheme",
        "/issuetypescheme/mapping",
        "/issuetypescheme/project",
        "/issuetypescreenscheme",
        "/issuetypescreenscheme/project",
        "/fieldconfiguration",
        "/fieldconfigurationscheme",
        "/fieldconfigurationscheme/project",
    ):
        mock.get(f"{CLOUD_ROOT}{path}").mock(return_value=httpx.Response(200, json=_paginated([])))
    mock.get(f"{CLOUD_ROOT}/issuesecurityschemes").mock(
        return_value=httpx.Response(200, json={"issueSecuritySchemes": []})
    )
    if roles:
        from tests._stubs import stub_project_roles_and_permission_schemes

        stub_project_roles_and_permission_schemes(mock, CLOUD_ROOT)


@respx.mock
def test_autogenerate_greenfield_writes_full_migration(tmp_path, monkeypatch, capsys):
    """Empty state, empty Jira, schema = platform example → writes a migration
    that loads cleanly and contains the expected op calls."""
    _stub_empty_jira(respx.mock)

    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "initial platform schema",
            "--autogenerate",
            "--schema",
            "examples.company_managed.platform_schema",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "operation(s) emitted" in out

    # Re-load via the migration loader — confirms the generated file is valid.
    graph = load_migrations(mig_dir)
    assert len(graph.by_revision) == 1
    new = next(iter(graph.by_revision.values()))
    assert new.down_revision is None

    # Inspect source for sanity.
    files = list(mig_dir.glob("*.py"))
    source = files[0].read_text(encoding="utf-8")
    assert "from stint import op" in source
    assert "from stint.fields import SelectField, TextField" in source
    assert "op.create_custom_field" in source
    assert "op.create_issuetype" in source
    assert "op.create_screen" in source
    assert "op.add_screen_tab_field" in source
    assert "op.create_screen_scheme" in source
    assert "op.create_field_configuration" in source
    assert "op.create_issuetype_scheme" in source
    assert "op.create_issuetype_screen_scheme" in source
    assert "op.create_field_configuration_scheme" in source
    assert "op.create_project" in source
    assert "op.set_project_issuetype_scheme" in source
    assert "op.set_project_issuetype_screen_scheme" in source
    assert "op.set_project_field_configuration_scheme" in source
    # Downgrade is the standard refusal:
    assert "op.unsupported(" in source


@respx.mock
def test_autogenerate_rechecks_head_after_reflect(tmp_path, monkeypatch, capsys):
    """L19: autogenerate captured head parents before the slow reflect; two
    concurrent runs could both parent to the same head and leave a branched
    graph that chain_from refuses to walk. Parents are now re-read from the
    graph immediately before writing."""
    import stint.cli.cmd_revision

    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    mig_dir.mkdir()
    (mig_dir / "seed.py").write_text(
        "revision = 'head_seeded'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n",
        encoding="utf-8",
    )

    real = stint.cli.cmd_revision._current_head_parents
    calls = {"n": 0}

    def flaky_parents(dir_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("stale_head",)  # lie: head captured before the reflect
        return real(dir_arg)

    monkeypatch.setattr("stint.cli.cmd_revision._current_head_parents", flaky_parents)
    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "initial platform schema",
            "--autogenerate",
            "--force",
            "--schema",
            "examples.company_managed.platform_schema",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    postgres = None
    graph = load_migrations(mig_dir)
    postgres = next(m for m in graph.by_revision.values() if m.revision != "head_seeded")
    # The stale head was not persisted; the real head was re-checked.
    assert calls["n"] >= 2
    assert postgres.down_revision == "head_seeded"


@respx.mock
def test_autogenerate_refuses_to_stack_pending_migrations(tmp_path, monkeypatch, capsys):
    """Running autogenerate twice without applying must not stack a duplicate
    migration. The second run refuses (#6); --force overrides."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("STINT_TOKEN", "tok")

    def run(message, *extra):
        return main(
            [
                "revision",
                "--migrations-dir",
                str(mig_dir),
                "-m",
                message,
                "--autogenerate",
                "--schema",
                "examples.company_managed.platform_schema",
                "--state",
                str(state_path),
                "--env",
                "dev",
                "--url",
                f"jira_cloud+{BASE}",
                "--auth",
                "pat",
                *extra,
            ]
        )

    assert run("initial platform schema") == 0
    capsys.readouterr()
    assert len(list(mig_dir.glob("*.py"))) == 1

    # Second run: migration pending, state never advanced -> refuse, no new file.
    assert run("second pass") == 1
    out = capsys.readouterr().out
    assert "pending migration" in out
    assert len(list(mig_dir.glob("*.py"))) == 1

    # --force stacks anyway (distinct message avoids the same-minute filename clash).
    assert run("forced stack", "--force") == 0
    assert len(list(mig_dir.glob("*.py"))) == 2


@respx.mock
def test_autogenerate_in_sync_writes_nothing(tmp_path, monkeypatch, capsys):
    """If state already matches schema (or schema is empty), no file is written."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "noop",
            "--autogenerate",
            # No schema declarations imported (registry empty due to fixture reset).
            "--schema",
            "stint.registry",  # any importable, no schema content
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    assert "in sync" in capsys.readouterr().out
    assert not mig_dir.exists() or not list(mig_dir.glob("*.py"))


@respx.mock
def test_autogenerate_orphan_state_warns_without_allow_delete(
    tmp_path,
    monkeypatch,
    capsys,
):
    """State has an alias that schema doesn't declare; without --allow-delete,
    autogenerate emits a warning and (since schema declares nothing else) no file."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import CustomFieldMapping

    state.custom_fields["orphan"] = CustomFieldMapping(id="customfield_99999")
    state.save(state_path)

    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "noop",
            "--autogenerate",
            "--schema",
            "stint.registry",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning:" in out
    assert "orphan" in out
    assert "in sync" in out


@respx.mock
def test_autogenerate_scopes_to_schema_module_projects(tmp_path, monkeypatch, capsys):
    """The --schema module imports a sibling module for reuse; the sibling's
    own Project registers as a side effect. The generated migration must not
    touch that project (CMP-B)."""
    _stub_empty_jira(respx.mock)
    (tmp_path / "schema_b.py").write_text(
        "from stint import IssueType, Project\n"
        "\n"
        "class TaskB(IssueType):\n"
        "    __alias__ = 'task_b'\n"
        "\n"
        "class ProjB(Project):\n"
        "    __key__ = 'BBB'\n"
        "    __style__ = 'company-managed'\n"
        "    __issuetypes__ = [TaskB]\n",
        encoding="utf-8",
    )
    (tmp_path / "schema_a.py").write_text(
        "import schema_b  # noqa: F401 -- reuse import; registers ProjB as a side effect\n"
        "from stint import IssueType, Project\n"
        "\n"
        "class TaskA(IssueType):\n"
        "    __alias__ = 'task_a'\n"
        "\n"
        "class ProjA(Project):\n"
        "    __key__ = 'AAA'\n"
        "    __style__ = 'company-managed'\n"
        "    __issuetypes__ = [TaskA]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "scoped autogen",
            "--autogenerate",
            "--schema",
            str(tmp_path / "schema_a.py"),
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "migration covers project(s): AAA" in out

    files = list(mig_dir.glob("*.py"))
    assert len(files) == 1
    src = files[0].read_text(encoding="utf-8")
    assert "key='AAA'" in src
    assert "alias='task_a'" in src
    assert "key='BBB'" not in src
    assert "alias='task_b'" not in src


@respx.mock
def test_autogenerate_allow_delete_emits_delete(tmp_path, monkeypatch, capsys):
    """Same as above but with --allow-delete: a delete op IS emitted."""
    _stub_empty_jira(respx.mock)
    mig_dir = tmp_path / "migrations"
    state_path = tmp_path / "state.yaml"
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import CustomFieldMapping

    state.custom_fields["orphan"] = CustomFieldMapping(id="customfield_99999")
    state.save(state_path)

    monkeypatch.setenv("STINT_TOKEN", "tok")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "drop orphan",
            "--autogenerate",
            "--schema",
            "stint.registry",
            "--state",
            str(state_path),
            "--env",
            "dev",
            "--url",
            f"jira_cloud+{BASE}",
            "--auth",
            "pat",
            "--allow-delete",
        ]
    )
    assert rc == 0
    files = list(mig_dir.glob("*.py"))
    assert len(files) == 1
    src = files[0].read_text(encoding="utf-8")
    assert "op.delete_custom_field(alias='orphan')" in src


@respx.mock
def test_autogenerate_lockout_guard_refuses_role_removal(tmp_path, monkeypatch, capsys):
    """Plan-time lockout guard: a __roles__ declaration that would remove the
    authenticated account's last role granting BROWSE/ADMINISTER refuses to
    write a migration unless --allow-lockout is passed."""
    _stub_empty_jira(respx.mock, roles=False)
    mock = respx.mock
    from tests._stubs import paginated

    mock.get(f"{CLOUD_ROOT}/myself").mock(return_value=httpx.Response(200, json={"accountId": "acct-svc"}))
    mock.get(f"{CLOUD_ROOT}/user").mock(return_value=httpx.Response(200, json={"groups": {"size": 0, "items": []}}))
    mock.get(f"{CLOUD_ROOT}/project/search").mock(
        return_value=httpx.Response(
            200,
            json=paginated(
                [
                    {
                        "id": "10010",
                        "key": "PLAT",
                        "name": "Platform",
                        "projectTypeKey": "software",
                        "style": "classic",
                        "description": "",
                    }
                ]
            ),
        )
    )
    mock.get(f"{CLOUD_ROOT}/project/10010/role").mock(
        return_value=httpx.Response(
            200,
            json={"Administrators": f"{CLOUD_ROOT}/project/10010/role/10360"},
        )
    )
    mock.get(f"{CLOUD_ROOT}/project/10010/role/10360").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "10360",
                "name": "Administrators",
                "actors": [{"id": "a1", "type": "atlassian-user-role-actor", "actorUser": {"accountId": "acct-svc"}}],
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/project/10010/permissionscheme").mock(
        return_value=httpx.Response(200, json={"id": "20000", "name": "Default"})
    )
    mock.get(f"{CLOUD_ROOT}/permissionscheme").mock(
        return_value=httpx.Response(
            200,
            json={
                "permissionSchemes": [
                    {
                        "id": 20000,
                        "name": "Default",
                        "permissions": [
                            {
                                "id": "g1",
                                "permission": "BROWSE_PROJECTS",
                                "holder": {"type": "projectRole", "parameter": "10360"},
                            }
                        ],
                    }
                ]
            },
        )
    )
    mock.get(f"{CLOUD_ROOT}/role").mock(
        return_value=httpx.Response(200, json=paginated([{"id": "10360", "name": "Administrators"}]))
    )
    mock.get(f"{CLOUD_ROOT}/issuesecurityschemes/project").mock(return_value=httpx.Response(200, json=paginated([])))

    # A pre-existing state so the project takes the "existing project" diff
    # path (where removals are planned).
    state_path = tmp_path / "state.yaml"
    state = StateFile(env="dev", jira_url=BASE)
    from stint.state.file import ProjectMapping

    state.projects["PLAT"] = ProjectMapping(id="10010", style="company-managed", key="PLAT")
    state.save(state_path)

    schema_path = tmp_path / "roles_schema.py"
    schema_path.write_text(
        "from stint import IssueType, Project, Group\n\n"
        "class Bug(IssueType):\n    __alias__ = 'bug'\n\n"
        "class Tracker(Project):\n"
        "    __key__ = 'PLAT'\n"
        "    __issuetypes__ = [Bug]\n"
        "    __roles__ = {'Administrators': [Group('contractors')]}\n",
        encoding="utf-8",
    )

    mig_dir = tmp_path / "migrations"
    monkeypatch.setenv("STINT_TOKEN", "tok")
    common = [
        "revision",
        "--migrations-dir",
        str(mig_dir),
        "-m",
        "lock roles",
        "--autogenerate",
        "--schema",
        str(schema_path),
        "--state",
        str(state_path),
        "--env",
        "dev",
        "--url",
        f"jira_cloud+{BASE}",
        "--auth",
        "pat",
        "--allow-delete",
    ]
    rc = main(common)
    assert rc == 1
    out = capsys.readouterr().out
    assert "lock the authenticated account" in out
    assert "--allow-lockout" in out
    assert not list(mig_dir.glob("*.py"))

    # The escape hatch writes the migration with allow_lockout=True baked in.
    rc = main(common + ["--allow-lockout"])
    assert rc == 0
    src = list(mig_dir.glob("*.py"))[0].read_text(encoding="utf-8")
    assert "op.remove_project_role_actor(" in src
    assert "allow_lockout=True" in src
