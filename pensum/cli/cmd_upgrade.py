"""`pensum upgrade`, `downgrade`, `current`, `history`: migration runners."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from cyclopts import Parameter

from pensum.cli.app import app
from pensum.cli.cmd_reflect import _build_auth
from pensum.cli.env_config import load_env_config
from pensum.engine import create_engine
from pensum.migrations.loader import load_migrations
from pensum.migrations.runner import downgrade as run_downgrade
from pensum.migrations.runner import upgrade as run_upgrade
from pensum.state.file import StateFile
from pensum.state.lock import StateLock

AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_dc", "jira_cloud"]


@app.command
async def upgrade(
    *,
    migrations_dir: Annotated[str, Parameter(help="Path to the migrations directory.")],
    state: Annotated[str, Parameter(help="Path to the state file (created if absent).")],
    env: Annotated[str, Parameter(help="Logical environment name; recorded in state.")],
    url: Annotated[str | None, Parameter(help="Jira URL. Read from env config if omitted.")] = None,
    dialect: Annotated[DialectName | None, Parameter()] = None,
    auth: Annotated[
        AuthMode | None,
        Parameter(help="Auth scheme. Read from env config if omitted."),
    ] = None,
    token_env: Annotated[
        str | None,
        Parameter(help="Env var holding the secret. Read from env config if omitted."),
    ] = None,
    user_env: Annotated[
        str | None,
        Parameter(help="Env var holding the username/email. Read from env config if omitted."),
    ] = None,
    no_verify_ssl: Annotated[bool, Parameter(negative=())] = False,
    to: Annotated[str | None, Parameter(help="Stop at a specific revision (default: head).")] = None,
) -> int:
    """Run pending migrations against an env."""
    url, auth, dialect, token_env, user_env, no_verify_ssl = _resolve_connection(
        env=env,
        url=url,
        auth=auth,
        dialect=dialect,
        token_env=token_env,
        user_env=user_env,
        no_verify_ssl=no_verify_ssl,
    )
    _require_resolved_connection(env=env, url=url, auth=auth)
    graph = load_migrations(migrations_dir)
    state_file = _load_or_init_state(state, env=env, jira_url=url)
    auth_obj = _build_auth(auth, token_env, user_env)
    engine = create_engine(url, auth=auth_obj, dialect=dialect, verify_ssl=not no_verify_ssl)
    lock = StateLock(state)
    lock.acquire()
    try:
        applied = await run_upgrade(engine, state_file, graph, state, target=to)
    finally:
        await engine.close()
        lock.release()
    if not applied:
        print(f"already at revision {state_file.revision!r}; nothing to do")
        return 0
    for m in applied:
        print(f"applied {m.revision[:8]} ({m.description})")
    print(f"now at revision {state_file.revision!r}")
    return 0


@app.command
async def downgrade(
    *,
    migrations_dir: Annotated[str, Parameter()],
    state: Annotated[str, Parameter(help="Path to the state file.")],
    env: Annotated[str, Parameter(help="Logical environment name; recorded in state.")],
    revision: Annotated[
        str,
        Parameter(name=("--revision", "-r"), help="Target revision. Use 'base' to roll back everything."),
    ],
    url: Annotated[str | None, Parameter()] = None,
    dialect: Annotated[DialectName | None, Parameter()] = None,
    auth: Annotated[AuthMode | None, Parameter()] = None,
    token_env: Annotated[
        str | None,
        Parameter(help="Env var holding the secret. Read from env config if omitted."),
    ] = None,
    user_env: Annotated[
        str | None,
        Parameter(help="Env var holding the username/email. Read from env config if omitted."),
    ] = None,
    no_verify_ssl: Annotated[bool, Parameter(negative=())] = False,
) -> int:
    """Roll back to a prior revision."""
    url, auth, dialect, token_env, user_env, no_verify_ssl = _resolve_connection(
        env=env,
        url=url,
        auth=auth,
        dialect=dialect,
        token_env=token_env,
        user_env=user_env,
        no_verify_ssl=no_verify_ssl,
    )
    _require_resolved_connection(env=env, url=url, auth=auth)
    graph = load_migrations(migrations_dir)
    state_file = _load_or_init_state(state, env=env, jira_url=url)
    auth_obj = _build_auth(auth, token_env, user_env)
    engine = create_engine(url, auth=auth_obj, dialect=dialect, verify_ssl=not no_verify_ssl)
    target: str | None = None if revision == "base" else revision
    lock = StateLock(state)
    lock.acquire()
    try:
        reversed_migrations = await run_downgrade(
            engine,
            state_file,
            graph,
            state,
            target=target,
        )
    finally:
        await engine.close()
        lock.release()
    if not reversed_migrations:
        print(f"already at revision {state_file.revision!r}; nothing to do")
        return 0
    for m in reversed_migrations:
        print(f"reversed {m.revision[:8]} ({m.description})")
    print(f"now at revision {state_file.revision!r}")
    return 0


@app.command
def current(*, state: Annotated[str, Parameter(help="Path to the state file.")]) -> int:
    """Show the current revision recorded in the state file."""
    state_path = Path(state)
    if not state_path.exists():
        print("(no state file; treat as base / before any migrations)")
        return 0
    state_file = StateFile.load(state_path)
    print(state_file.revision if state_file.revision else "(base)")
    return 0


@app.command
def history(*, migrations_dir: Annotated[str, Parameter(help="Path to the migrations directory.")]) -> int:
    """List all migrations in revision order."""
    graph = load_migrations(migrations_dir)
    for m in graph.all_ordered():
        parent = m.down_revision[:8] if m.down_revision else "(base)"
        print(f"{m.revision[:8]}  parent={parent}  {m.description}")
    return 0


def _resolve_connection(
    *,
    env: str,
    url: str | None,
    auth: str | None,
    dialect: str | None,
    token_env: str | None,
    user_env: str | None,
    no_verify_ssl: bool,
) -> tuple[str | None, str | None, str | None, str, str, bool]:
    """Merge env-config values for any connection params the caller did not set.

    ``token_env`` / ``user_env`` are returned as concrete strings (never None):
    the YAML value wins over the default when no CLI flag is set, but
    something has to be returned for the env-var lookup.
    """
    cfg = load_env_config(env)
    if not cfg:
        return url, auth, dialect, token_env or "PENSUM_TOKEN", user_env or "PENSUM_USER", no_verify_ssl
    if not url:
        url = cfg.get("url")
    if not auth:
        auth = cfg.get("auth")
    if not dialect:
        dialect = cfg.get("dialect")
    if token_env is None:
        token_env = cfg.get("token_env") or "PENSUM_TOKEN"
    if user_env is None:
        user_env = cfg.get("user_env") or "PENSUM_USER"
    if "verify_ssl" in cfg and not cfg["verify_ssl"] and not no_verify_ssl:
        no_verify_ssl = True
    return url, auth, dialect, token_env, user_env, no_verify_ssl


def _require_resolved_connection(*, env: str, url: str | None, auth: str | None) -> None:
    missing = [k for k, v in (("url", url), ("auth", auth)) if not v]
    if missing:
        raise SystemExit(
            f"missing required connection params: {missing}. "
            f"Provide via --{'/--'.join(missing)} or place a config at "
            f"./.pensum/{env}.yaml or ~/.pensum/envs/{env}.yaml."
        )


def _load_or_init_state(path: str, *, env: str, jira_url: str) -> StateFile:
    p = Path(path)
    if p.exists():
        return StateFile.load(p)
    return StateFile(env=env, jira_url=jira_url)
