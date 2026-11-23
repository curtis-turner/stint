"""`stint check`: runtime schema check against a live tenant.

Imports the user's schema module, loads the state file, then runs
`engine.check(project, state)` for every registered project — reflect plus
createmeta, all reads, nothing applied. Exits non-zero if any project has
errors, so a service can use it as a startup gate.

Distinct from `stint validate` (offline, no network) and
`revision --autogenerate` (writes a migration file): this is the read-only
call a running service makes to fail fast when the tenant drifted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import Parameter

from stint.autogen.loader import load_schema_module
from stint.cli.app import app
from stint.cli.env_config import require_resolved_connection, resolve_connection
from stint.client.auth import APITokenAuth, Auth, BasicAuth, PATAuth
from stint.engine import create_engine
from stint.exceptions import ConfigurationError
from stint.registry import registry
from stint.state.file import StateFile

AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_cloud", "jira_cloud_tmp"]


@app.command(name="check")
async def check_cmd(
    *,
    schema: Annotated[
        str,
        Parameter(
            help="Dotted module path (e.g. examples.company_managed.platform) or file path (./schemas/platform.py)."
        ),
    ],
    state: Annotated[
        str,
        Parameter(help="Path to the state file (state.yaml) for the environment being checked."),
    ],
    env: Annotated[
        str | None,
        Parameter(help="Env config name; reads connection params from ~/.stint/envs/<env>.yaml."),
    ] = None,
    url: Annotated[str | None, Parameter(help="Base URL. Read from env config if omitted.")] = None,
    auth: Annotated[
        AuthMode | None,
        Parameter(help="Auth mode (pat / basic / api-token). Read from env config if omitted."),
    ] = None,
    token_env: Annotated[str | None, Parameter(help="Env var holding the secret.")] = None,
    user_env: Annotated[str | None, Parameter(help="Env var holding the username/email.")] = None,
    no_verify_ssl: Annotated[
        bool,
        Parameter(negative=(), help="Skip TLS verification. Use only with internal/self-signed instances."),
    ] = False,
) -> int:
    """Check a declared project against the live tenant (read-only)."""
    try:
        load_schema_module(schema)
    except ConfigurationError as e:
        print(f"ERROR: {e}")
        return 1

    if not registry.projects:
        print(f"ERROR: {schema} declares no Project; nothing to check.")
        return 1

    url_, auth_, dialect_, token_env_, user_env_, no_verify_ssl_ = resolve_connection(
        env=env,
        url=url,
        auth=auth,
        dialect=None,
        token_env=token_env,
        user_env=user_env,
        no_verify_ssl=no_verify_ssl,
    )
    url_, auth_ = require_resolved_connection(env=env, url=url_, auth=auth_)
    if dialect_ == "jira_cloud_tmp":
        raise SystemExit("stint check supports company-managed projects only (jira_cloud).")
    auth_obj = _build_auth(auth_, token_env_ or "STINT_TOKEN", user_env_ or "STINT_USER")
    engine = create_engine(url_, auth=auth_obj, dialect="jira_cloud", verify_ssl=not no_verify_ssl_)
    state_file = StateFile.load(Path(state))

    any_errors = False
    try:
        for project_cls in registry.projects.values():
            report = await engine.check(project_cls, state_file)
            if report.ok and not report.warnings:
                print(f"{report.project}: OK")
                continue
            for warning in report.warnings:
                print(f"{report.project}: warning: {warning}")
            for error in report.errors:
                print(f"{report.project}: error: {error}")
            if report.ok:
                print(f"{report.project}: OK ({len(report.warnings)} warning(s))")
            else:
                any_errors = True
    finally:
        await engine.close()

    if any_errors:
        print("check failed: see errors above.")
        return 1
    return 0


def _build_auth(auth: str, token_env: str, user_env: str) -> Auth:
    """Resolve the auth scheme to a concrete Auth instance from env vars."""
    token = os.environ.get(token_env)
    user = os.environ.get(user_env)
    if auth == "pat":
        if not token:
            raise SystemExit(f"--auth pat requires ${token_env} to be set")
        return PATAuth(token=token)
    if auth == "basic":
        if not (user and token):
            raise SystemExit(f"--auth basic requires ${user_env} and ${token_env} to be set")
        return BasicAuth(username=user, password=token)
    if auth == "api-token":
        if not (user and token):
            raise SystemExit(f"--auth api-token requires ${user_env} (email) and ${token_env} (token) to be set")
        return APITokenAuth(email=user, token=token)
    raise SystemExit(f"unknown auth mode: {auth}")
