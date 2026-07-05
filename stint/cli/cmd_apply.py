"""`stint apply`: reflect + diff + write, in one run, for reconcile-style
dialects (currently only ``jira_cloud_tmp``).

Deliberately not folded into `stint upgrade`: upgrade's model is a revision
graph of incremental migration files, replayed forward/backward. TMP has no
such thing -- every write is full-replacement (see
`stint.dialects.jira.tmp.reconcile`'s module docstring), so there is nothing
to record as a migration step and no inverse to run on downgrade. `apply`
instead reflects the live project, diffs it against the schema right now,
and writes whatever changed -- closer to `terraform apply` than
`alembic upgrade`. Named generically (not `tmp-apply`) since this reconcile
style isn't inherently Jira-TMP-specific -- a future dialect with the same
no-migration-file, full-replacement write model would use this same verb.

Terraform-style safety: prints the plan, then requires interactive
confirmation (type "yes") before writing, since TMP writes have no undo.
`--auto-approve` skips the prompt for scripted/CI use. `--dry-run` prints the
plan and stops, no prompt.

All ``stint.dialects.jira.tmp`` imports below are deferred to inside
``apply()``'s body, not hoisted to module level -- ``stint/cli/main.py``
imports every ``cmd_*`` module unconditionally to register its subcommand,
so a module-level import here would mean any ``stint`` invocation at all
(even ``stint upgrade`` for a pure CMP user) loads the TMP package. Same
reasoning as ``stint/engine.py``'s ``create_tmp_engine``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from cyclopts import Parameter

from stint.autogen.loader import load_schema_module
from stint.cli.app import app
from stint.cli.cmd_reflect import _build_auth
from stint.cli.env_config import require_resolved_connection, resolve_connection
from stint.engine import create_tmp_engine, resolve_dialect_name
from stint.exceptions import ConfigurationError
from stint.registry import registry
from stint.state.file import ProjectMapping, StateFile
from stint.state.lock import StateLock

if TYPE_CHECKING:
    from stint.dialects.jira.tmp.reconcile import TmpChange

AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_cloud", "jira_cloud_tmp"]


@app.command
async def apply(
    *,
    schema: Annotated[str, Parameter(help="Schema module to import (dotted or file).")],
    project_key: Annotated[str, Parameter(help="Jira key of the team-managed project to reconcile.")],
    state: Annotated[str, Parameter(help="Path to the state file (created if absent).")],
    env: Annotated[str, Parameter(help="Logical environment name; recorded in state.")],
    url: Annotated[
        str | None,
        Parameter(help="Jira URL (with jira_cloud_tmp+ prefix accepted). Read from env config if omitted."),
    ] = None,
    auth: Annotated[
        AuthMode | None,
        Parameter(help="Auth scheme. Read from env config if omitted."),
    ] = None,
    dialect: Annotated[
        DialectName | None,
        Parameter(help="Dialect to use. Overrides any prefix in --url. Only jira_cloud_tmp is supported today."),
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
    allow_delete: Annotated[
        bool,
        Parameter(negative=(), help="Allow deleting fields/work types no longer declared. No undo."),
    ] = False,
    dry_run: Annotated[
        bool,
        Parameter(negative=(), help="Print the plan and stop. No prompt, no writes."),
    ] = False,
    auto_approve: Annotated[
        bool,
        Parameter(negative=(), help="Skip the interactive confirmation prompt. For scripted/CI use."),
    ] = False,
) -> int:
    """Reflect a team-managed project, diff it against schema, and apply."""
    from stint.dialects.jira.tmp.desired import build_tmp_desired
    from stint.dialects.jira.tmp.ops import TmpApplyContext
    from stint.dialects.jira.tmp.reconcile import apply_tmp_plan, plan_tmp
    from stint.dialects.jira.tmp.state import TmpState, from_project_state, to_project_state

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
    base_url, chosen_dialect = resolve_dialect_name(url, dialect)
    if chosen_dialect != "jira_cloud_tmp":
        raise ConfigurationError(f"stint apply does not yet support dialect {chosen_dialect!r}; only 'jira_cloud_tmp'.")

    load_schema_module(schema)
    project_cls = registry.projects.get(project_key)
    if project_cls is None:
        raise ConfigurationError(f"no Project with __key__ {project_key!r} found in schema {schema!r}")
    if getattr(project_cls, "__style__", "company-managed") != "team-managed":
        raise ConfigurationError(
            f"project {project_key!r} is company-managed; stint apply only supports team-managed "
            "projects (use 'stint upgrade' for company-managed schema)."
        )
    desired = build_tmp_desired(project_cls)

    auth_obj = _build_auth(auth, token_env, user_env)
    state_path = Path(state)
    state_file = StateFile.load(state_path) if state_path.exists() else StateFile(env=env, jira_url=base_url)
    tmp_state = (
        from_project_state(state_file.tmp_projects[project_key])
        if project_key in state_file.tmp_projects
        else TmpState()
    )

    lock = StateLock(state)
    lock.acquire()
    tmp_eng = create_tmp_engine(base_url, auth=auth_obj, dialect=chosen_dialect, verify_ssl=not no_verify_ssl)
    try:
        project_ctx = await tmp_eng.dialect.resolve_project(project_key=project_key)
        snapshot = await tmp_eng.dialect.reflect(project_key=project_key)
        changes = plan_tmp(desired, snapshot, tmp_state, allow_delete=allow_delete)

        if not changes:
            print("No changes. The team-managed project already matches the schema.")
            return 0

        _print_plan(changes)

        if dry_run:
            return 0
        if not auto_approve and not _confirm_apply():
            print("Apply cancelled.")
            return 1

        ctx = TmpApplyContext(dialect=tmp_eng.dialect, project=project_ctx, state=tmp_state)
        await apply_tmp_plan(ctx, changes, desired, snapshot)

        state_file.tmp_projects[project_key] = to_project_state(tmp_state)
        state_file.projects[project_key] = ProjectMapping(
            id=project_ctx.project_id, style="team-managed", key=project_ctx.key
        )
        state_file.save(state_path)
    finally:
        await tmp_eng.close()
        lock.release()

    _print_summary(changes)
    print(f"wrote {state_path}")
    return 0


def _change_counts(changes: list[TmpChange]) -> tuple[int, int, int]:
    symbols = [_describe_change(c)[0] for c in changes]
    return symbols.count("+"), symbols.count("~"), symbols.count("-")


def _describe_change(change: TmpChange) -> tuple[str, str]:
    """Return (symbol, label) for one change. isinstance dispatch, matching
    apply_tmp_plan's own dispatch style on this same Union type."""
    from stint.dialects.jira.tmp.reconcile import (
        CreateField,
        CreateWorkType,
        DeleteField,
        DeleteWorkType,
        SetLayout,
        UpdateField,
    )

    if isinstance(change, CreateField):
        return "+", "create_field"
    if isinstance(change, UpdateField):
        return "~", "update_field"
    if isinstance(change, DeleteField):
        return "-", "delete_field"
    if isinstance(change, CreateWorkType):
        return "+", "create_worktype"
    if isinstance(change, DeleteWorkType):
        return "-", "delete_worktype"
    if isinstance(change, SetLayout):
        return "~", "set_layout"
    raise AssertionError(f"unhandled TmpChange type: {type(change)!r}")


def _print_plan(changes: list[TmpChange]) -> None:
    adds, updates, deletes = _change_counts(changes)
    print(f"Plan: {adds} to add, {updates} to change, {deletes} to destroy.")
    print()
    for change in changes:
        symbol, label = _describe_change(change)
        print(f"  {symbol} {label:<16} {change.alias}")
    print()


def _print_summary(changes: list[TmpChange]) -> None:
    adds, updates, deletes = _change_counts(changes)
    print(f"Apply complete! Resources: {adds} added, {updates} changed, {deletes} destroyed.")


def _confirm_apply() -> bool:
    print("Do you want to perform these actions?")
    print("  stint will make the changes described above.")
    print("  Only 'yes' will be accepted to approve.")
    print()
    response = input("Enter a value: ")
    return response.strip() == "yes"
