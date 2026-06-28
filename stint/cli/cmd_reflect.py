"""`stint reflect`: reflect a Jira instance and print the snapshot.

Output is YAML by default (most readable for the schemes-of-schemes data).
JSON optional for piping to other tools.

Auth comes from environment variables. Hardcoded secrets in flags would
inevitably leak into shell history.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from typing import Annotated, Any, Literal

import yaml
from cyclopts import Parameter

from stint.cli.app import app
from stint.cli.env_config import require_resolved_connection, resolve_connection
from stint.client.auth import APITokenAuth, BasicAuth, PATAuth
from stint.engine import create_engine

AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_cloud"]


@app.command
async def reflect(
    *,
    env: Annotated[
        str | None,
        Parameter(help="Env config name; reads connection params from ~/.stint/envs/<env>.yaml."),
    ] = None,
    url: Annotated[
        str | None,
        Parameter(help="Base URL. Read from env config if omitted."),
    ] = None,
    auth: Annotated[
        AuthMode | None,
        Parameter(
            help=(
                "Auth mode. pat=Personal Access Token (DC), basic=username+password, "
                "api-token=email+API token (Cloud). Read from env config if omitted."
            ),
        ),
    ] = None,
    dialect: Annotated[
        DialectName | None,
        Parameter(help="Dialect to use. Overrides any prefix in --url. Required if --url has no prefix."),
    ] = None,
    token_env: Annotated[
        str | None,
        Parameter(help="Env var holding the secret. Read from env config if omitted."),
    ] = None,
    user_env: Annotated[
        str | None,
        Parameter(help="Env var holding the username/email. Read from env config if omitted."),
    ] = None,
    format: Annotated[Literal["yaml", "json"], Parameter(help="Output format.")] = "yaml",
    no_verify_ssl: Annotated[
        bool,
        Parameter(
            negative=(),
            help="Skip TLS verification. Use only with internal/self-signed DC instances.",
        ),
    ] = False,
) -> int:
    """Reflect a Jira instance into a snapshot and print it."""
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
    auth_obj = _build_auth(auth, token_env, user_env)
    eng = create_engine(url, auth=auth_obj, dialect=dialect, verify_ssl=not no_verify_ssl)
    try:
        snapshot = await eng.reflect()
    finally:
        await eng.close()

    serialized = _to_serializable(snapshot)
    if format == "yaml":
        print(yaml.safe_dump(serialized, sort_keys=False), end="")
    else:
        print(json.dumps(serialized, indent=2))
    return 0


def _build_auth(auth: str, token_env: str, user_env: str) -> Any:
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


def _to_serializable(obj: Any) -> Any:
    """Recursively convert dataclasses and tuples to dicts/lists for YAML/JSON."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return _to_serializable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(v) for v in obj]
    return obj
