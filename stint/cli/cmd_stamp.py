"""`stint stamp`: brownfield — bring existing Jira objects into state.

Reflects the target env, matches each schema-declared alias to a Jira object
by name (or key for projects), populates the state file. No writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from cyclopts import Parameter

from stint.autogen.loader import load_schema_module
from stint.autogen.stamp import stamp as run_stamp_alg
from stint.cli.app import app
from stint.cli.cmd_reflect import _build_auth
from stint.cli.env_config import require_resolved_connection, resolve_connection
from stint.engine import create_engine
from stint.state.file import StateFile

AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_cloud"]


@app.command
async def stamp(
    *,
    schema: Annotated[str, Parameter(help="Schema module to import (dotted or file).")],
    state: Annotated[str, Parameter(help="State file to populate (created if absent).")],
    env: Annotated[str, Parameter(help="Logical env name; recorded in state.")],
    url: Annotated[
        str | None,
        Parameter(help="Jira URL (with dialect prefix accepted). Read from env config if omitted."),
    ] = None,
    auth: Annotated[
        AuthMode | None,
        Parameter(help="Auth scheme. Read from env config if omitted."),
    ] = None,
    dialect: Annotated[DialectName | None, Parameter()] = None,
    token_env: Annotated[
        str | None,
        Parameter(help="Env var holding the secret. Read from env config if omitted."),
    ] = None,
    user_env: Annotated[
        str | None,
        Parameter(help="Env var holding the username/email. Read from env config if omitted."),
    ] = None,
    no_verify_ssl: Annotated[bool, Parameter(negative=())] = False,
    revision: Annotated[
        str | None,
        Parameter(help="Mark the state file at this revision id (default: leave unchanged)."),
    ] = None,
) -> int:
    """Brownfield: populate state by matching schema declarations to existing Jira objects."""
    url, auth, dialect, token_env, user_env, no_verify_ssl = resolve_connection(
        env=env,
        url=url,
        auth=auth,
        dialect=dialect,
        token_env=token_env,
        user_env=user_env,
        no_verify_ssl=no_verify_ssl,
    )
    url, auth = require_resolved_connection(env=env, url=url, auth=auth)
    load_schema_module(schema)
    state_path = Path(state)
    state_file = StateFile.load(state_path) if state_path.exists() else StateFile(env=env, jira_url=url)
    auth_obj = _build_auth(auth, token_env, user_env)
    engine = create_engine(url, auth=auth_obj, dialect=dialect, verify_ssl=not no_verify_ssl)
    try:
        snapshot = await engine.reflect()
    finally:
        await engine.close()
    report = run_stamp_alg(state_file, snapshot)
    if revision:
        state_file.revision = revision
    state_file.save(state_path)

    for kind, alias, jira_id in report.matched:
        print(f"matched   {kind:<28} {alias!r:<30}-> {jira_id}")
    for kind, alias in report.unmatched:
        print(f"unmatched {kind:<28} {alias!r}")
    for kind, alias, reason in report.skipped:
        print(f"skipped   {kind:<28} {alias!r}: {reason}")
    print()
    print(f"matched={len(report.matched)} unmatched={len(report.unmatched)} skipped={len(report.skipped)}")
    print(f"wrote {state_path}")
    return 0
