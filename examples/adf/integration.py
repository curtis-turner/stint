import asyncio

from stint import StateFile
from stint.cli.cmd_reflect import _build_auth
from stint.cli.env_config import require_resolved_connection, resolve_connection
from stint.engine import create_engine
from stint.query.adf import parse_text
from stint.query.hydrate import hydrate
from stint.query.payload import build_insert_payload
from stint.schema.issuetype import IssueType
from stint.state.file import ProjectMapping, SimpleMapping


# --- Minimal schema for the integration test ---------------------------------
class SimpleIssue(IssueType):
    __alias__ = "bug"
    summary: str
    description: str | None = None


# --- Main integration flow ---------------------------------------------------
async def main():
    env_name = "devel"  # use the .stint/devel.yaml config file

    # Resolve connection parameters (url, auth, dialect)
    url, auth, dialect, token_env, user_env, no_verify_ssl = resolve_connection(
        env=env_name,
        url=None,
        auth=None,
        dialect=None,
        token_env=None,
        user_env=None,
        no_verify_ssl=False,
    )
    url, auth = require_resolved_connection(env=env_name, url=url, auth=auth)
    auth_obj = _build_auth(auth, token_env, user_env)

    # Build engine
    engine = create_engine(url, auth=auth_obj, dialect=dialect, verify_ssl=not no_verify_ssl)

    # --- Ensure project exists and create state mapping -------------------------------------------------
    project_key = "TEST"
    # In‑memory state object; no persistence needed for this demo
    state_file = StateFile(env=env_name, jira_url=url, projects={}, issuetypes={})
    # Create in‑memory state file with project and issuetype mappings
    # Ensure project mapping exists
    if project_key not in state_file.projects:
        # Create project as team‑managed (next‑gen) without a lead
        created_proj = await engine.dialect.create_project(
            key=project_key,
            name="Test Project for ADF integration",
            project_type_key="software",
            lead="cjturner714@gmail.com",
        )
        state_file.projects[project_key] = ProjectMapping(id=created_proj, style="company-managed", key=project_key)

    # Ensure issuetype mapping exists
    if "bug" not in state_file.issuetypes:
        # Fetch the ID of the built‑in Foo issuetype
        issuetype_id = await engine.dialect.find_issuetype_id_by_name("Foo")
        if not issuetype_id:
            raise RuntimeError("Could not find issuetype 'Foo' in Jira")
        state_file.issuetypes["bug"] = SimpleMapping(id=issuetype_id)

    # Create an issue
    issue = SimpleIssue(summary="Live ADF test", description="Live line 1\n\nLive line 2")
    payload = build_insert_payload(issue, state_file, is_cloud=True, project_key=project_key)
    created = await engine.dialect.create_issue(payload)
    print("Created issue key:", created.get("key"))

    # Fetch the issue back and hydrate
    fetched = await engine.dialect.get_issue(created["key"], fields=list(payload["fields"].keys()))
    hydrated = hydrate(SimpleIssue, fetched, state_file)
    print("\nHydrated instance:")
    print(hydrated)

    # Parse the ADF description back to plain text
    print("\nParsed description:")
    print(parse_text(payload["fields"]["description"]))


if __name__ == "__main__":
    asyncio.run(main())
