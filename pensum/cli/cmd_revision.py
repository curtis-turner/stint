"""`pensum revision`: emit a new migration file.

Three modes:
  - default: empty skeleton with current head as parent
  - --merge: merge migration joining the listed heads
  - --autogenerate: reflect target, diff against schema, emit ops (M3 autogen)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Literal

from cyclopts import Parameter

from pensum.autogen.emit import new_filename, new_revision_id, render_empty, render_merge
from pensum.cli.app import app
from pensum.cli.cmd_reflect import _build_auth
from pensum.cli.env_config import require_resolved_connection, resolve_connection
from pensum.migrations.exceptions import MigrationGraphError
from pensum.migrations.loader import load_migrations

AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_cloud"]


@app.command
def revision(
    *,
    migrations_dir: Annotated[str, Parameter(help="Path to the migrations directory.")],
    message: Annotated[str, Parameter(name=("--message", "-m"), help="Migration description.")],
    merge: Annotated[
        list[str] | None,
        Parameter(
            consume_multiple=True,
            negative_iterable=(),
            help="Create a merge migration joining the named heads (space-separated revisions).",
        ),
    ] = None,
    autogenerate: Annotated[
        bool,
        Parameter(negative=(), help="Reflect target env, diff against schema, emit ops."),
    ] = False,
    schema: Annotated[
        str | None,
        Parameter(help="Python module path to load schema from (autogen)."),
    ] = None,
    state: Annotated[str | None, Parameter(help="State file path (autogen).")] = None,
    url: Annotated[
        str | None,
        Parameter(help="Jira URL (autogen). Read from env config if omitted."),
    ] = None,
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
    env: Annotated[
        str | None,
        Parameter(
            help="Env name (autogen). Recorded in the state file; reads connection params from env config if set."
        ),
    ] = None,
    allow_delete: Annotated[
        bool,
        Parameter(
            negative=(),
            help="Permit autogenerate to emit destructive ops (delete_*, remove_custom_field_option).",
        ),
    ] = False,
) -> int:
    """Create a new migration file."""
    if autogenerate and merge:
        print("ERROR: --autogenerate and --merge are mutually exclusive")
        return 2
    if autogenerate:
        return _run_autogenerate(
            migrations_dir=migrations_dir,
            message=message,
            schema=schema,
            state=state,
            url=url,
            dialect=dialect,
            auth=auth,
            token_env=token_env,
            user_env=user_env,
            no_verify_ssl=no_verify_ssl,
            env=env,
            allow_delete=allow_delete,
        )
    if merge:
        return _run_merge(migrations_dir=migrations_dir, message=message, merge=merge)
    return _run_empty(migrations_dir=migrations_dir, message=message)


def _run_empty(*, migrations_dir: str, message: str) -> int:
    mig_dir = Path(migrations_dir)
    parents = _current_head_parents(mig_dir)
    revision_id = new_revision_id()
    source = render_empty(message, parents=parents, revision=revision_id)
    return _write_migration(mig_dir, message, source, revision_id)


def _run_merge(*, migrations_dir: str, message: str, merge: list[str]) -> int:
    if len(merge) < 2:
        print("ERROR: --merge requires at least two revision ids")
        return 2
    mig_dir = Path(migrations_dir)
    graph = load_migrations(mig_dir)
    head_revs = {h.revision for h in graph.heads()}
    missing = [r for r in merge if r not in graph.by_revision]
    if missing:
        print(f"ERROR: revisions not found in graph: {missing}")
        return 2
    non_heads = [r for r in merge if r not in head_revs]
    if non_heads:
        print(f"ERROR: revisions are not current heads: {non_heads}. Current heads: {sorted(head_revs)}")
        return 2
    revision_id = new_revision_id()
    source = render_merge(message, parents=tuple(merge), revision=revision_id)
    return _write_migration(mig_dir, message, source, revision_id)


def _run_autogenerate(
    *,
    migrations_dir: str,
    message: str,
    schema: str | None,
    state: str | None,
    url: str | None,
    dialect: str | None,
    auth: str | None,
    token_env: str | None,
    user_env: str | None,
    no_verify_ssl: bool,
    env: str | None,
    allow_delete: bool,
) -> int:
    from pensum.autogen.desired import build_desired_snapshot
    from pensum.autogen.diff import diff
    from pensum.autogen.emit import render_autogenerated
    from pensum.autogen.loader import load_schema_module
    from pensum.engine import create_engine
    from pensum.state.file import StateFile

    required = {"schema": schema, "state": state, "env": env}
    for name, value in required.items():
        if not value:
            print(f"ERROR: --autogenerate requires --{name}")
            return 2
    url, auth, dialect, token_env, user_env, no_verify_ssl = resolve_connection(
        env=env,
        url=url,
        auth=auth,
        dialect=dialect,
        token_env=token_env,
        user_env=user_env,
        no_verify_ssl=no_verify_ssl,
    )
    require_resolved_connection(env=env, url=url, auth=auth)

    mig_dir = Path(migrations_dir)
    state_path = Path(state)  # checked above
    state_file = StateFile.load(state_path) if state_path.exists() else StateFile(env=env, jira_url=url)
    load_schema_module(schema)
    desired = build_desired_snapshot()
    parents = _current_head_parents(mig_dir)
    revision_id = new_revision_id()

    async def reflect() -> Any:
        auth_obj = _build_auth(auth, token_env, user_env)
        engine = create_engine(url, auth=auth_obj, dialect=dialect, verify_ssl=not no_verify_ssl)
        try:
            return await engine.reflect()
        finally:
            await engine.close()

    snapshot = asyncio.run(reflect())
    result = diff(desired=desired, snapshot=snapshot, state=state_file, allow_delete=allow_delete)
    for w in result.warnings:
        print(f"warning: {w}")
    if not result.changes:
        print("no changes detected; schema and Jira are in sync")
        return 0

    source = render_autogenerated(
        message,
        parents=parents,
        revision=revision_id,
        changes=result.changes,
    )
    rc = _write_migration(mig_dir, message, source, revision_id)
    if rc == 0:
        print(f"  {len(result.changes)} operation(s) emitted")
    return rc


def _current_head_parents(mig_dir: Path) -> tuple[str, ...] | None:
    """Return (head_revision,) or None if there are no migrations yet."""
    if not mig_dir.exists() or not any(mig_dir.glob("*.py")):
        return None
    try:
        graph = load_migrations(mig_dir)
    except MigrationGraphError:
        raise
    heads = graph.heads()
    if not heads:
        return None
    if len(heads) > 1:
        raise MigrationGraphError(
            f"multiple heads exist: {sorted(h.revision for h in heads)}. Run `pensum revision --merge ...` first."
        )
    return (heads[0].revision,)


def _write_migration(mig_dir: Path, message: str, source: str, revision: str) -> int:
    mig_dir.mkdir(parents=True, exist_ok=True)
    path = mig_dir / new_filename(message)
    if path.exists():
        print(f"ERROR: target file already exists: {path}")
        return 1
    path.write_text(source)
    print(f"wrote {path}")
    print(f"  revision={revision}")
    return 0
