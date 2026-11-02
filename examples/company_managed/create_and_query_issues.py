"""Create issues in the SEC project and read them back through the ORM.

Runnable companion to `security_scheme.py`: proves the full end-to-end data
plane once the migration path has stood up the project — writes
(`session.add` -> `commit`), single reads (`session.get`), filtered reads
(`select` + `where`, translated to JQL), dirty tracking, and deletes.

Prerequisites (see README.md):
  - `stint upgrade` has been run for the `SEC` project on your tenant
  - `.stint/devel.yaml` points at your tenant, and STINT_USER / STINT_TOKEN
    are exported

Run it (from anywhere):

    STINT_USER='...' STINT_TOKEN='...' \
    uv run python examples/company_managed/create_and_query_issues.py

    # keep the created issues in Jira instead of cleaning them up:
    STINT_USER='...' STINT_TOKEN='...' \
    uv run python examples/company_managed/create_and_query_issues.py --keep
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Running a script by file path puts the script's directory at sys.path[0].
# This directory contains platform.py, which shadows the standard-library
# `platform` module (imported by attrs, via cyclopts, via stint.cli) and
# broke every import. Remove the script dir from the path up front — the
# schema loads by absolute file path below, and nothing here needs that
# directory on sys.path.
_SELF_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if entry != str(_SELF_DIR)]

from stint import StateFile  # noqa: E402  (imports must wait for the scrub)
from stint.cli.cmd_revision import _build_auth  # noqa: E402
from stint.cli.env_config import require_resolved_connection, resolve_connection  # noqa: E402
from stint.engine import create_engine  # noqa: E402
from stint.query import AsyncSession, or_, select  # noqa: E402
from stint.query.session import PartialCommitError  # noqa: E402


def _load_schema(schema_arg: str | None):
    """Load the schema module the same way the CLI does (works with file
    paths regardless of cwd) and return the module."""
    from stint.autogen.loader import load_schema_module

    default = Path(__file__).resolve().parent / "security_scheme.py"
    target = Path(schema_arg) if schema_arg else default
    return load_schema_module(str(target))


async def _resolve_caller_account_id(engine) -> str:
    """The ORM write path takes a raw accountId for reporter/assignee on
    Cloud (no email resolution there — that happens in migrations only).
    The API token identifies a real user, so whoami it."""
    payload = await engine.client.get_json(f"{engine.dialect.api_root}/myself")
    account_id = (payload or {}).get("accountId", "")
    if not account_id:
        raise RuntimeError(
            "GET /myself returned no accountId; cannot set `reporter`. Pass "
            "a real accountId directly into the Bug(...) constructor."
        )
    return str(account_id)


async def run(keep: bool, state_path: str, env_name: str, schema_target: str | None) -> int:
    schema = _load_schema(schema_target)

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
    engine = create_engine(
        url,
        auth=_build_auth(auth, token_env, user_env),
        dialect=dialect,
        verify_ssl=not no_verify_ssl,
    )
    state = StateFile.load(state_path)
    print(f"tenant: {url}")
    print(f"state:  {state_path} (at revision {state.revision})")
    print("note:   reporter is left unset — Jira takes it from the caller's identity on create")

    created_keys: list[str] = []
    try:
        async with AsyncSession(engine, state) as session:
            # ── WRITE: insert ─────────────────────────────────────────
            # Construct issues as plain Pydantic instances. Custom-field
            # values (severity, root_cause, ...) are declared on the schema;
            # at commit time stint translates them into cf[<jira_id>]
            # payloads using the state file's alias maps.
            #
            # reporter is left unset: Jira sets it from the API token's
            # identity on create (passing one in the payload 400s).
            bug_a = schema.Bug(
                summary="Query demo: S1 security bug",
                description="Demonstration issue A",
                severity="S1",
                test_text_field="demo",
            )
            bug_b = schema.Bug(
                summary="Query demo: S2 bug",
                severity="S2",
                root_cause=None,
            )
            session.add(bug_a)  # project inferred: SEC is Bug's only project
            session.add(bug_b, project="SEC")  # or pass the key explicitly
            try:
                results = await session.commit()
            except PartialCommitError as exc:
                for result in exc.results:
                    if result.error is not None:
                        print(f"  {result.operation} FAILED: {result.error}")
                raise
            for result in results:
                print(f"inserted: {result.instance.summary!r} -> {result.instance.key}")
                created_keys.append(result.instance.key)

            # ── READ: one issue by key ────────────────────────────────
            # get() also hydrates custom fields (severity etc.) resolved
            # through the state file's alias -> Jira id maps.
            #
            # fresh=True bypasses the session's identity map, which still
            # holds the instance insert() just created. Without it this
            # print proves nothing about server state: a create can return
            # a key while issue-level security hides the issue from the
            # same token (JQL empty, GET/PUT 404).
            first = await session.get(schema.Bug, created_keys[0], fresh=True)
            if first is None:
                print(
                    f"\nERROR: Jira accepted the create (key {created_keys[0]}) but "
                    f"the issue is not readable by this token.\n"
                    f"       The SEC security scheme's default level probably has no "
                    f"members, which hides every new issue from JQL and GET/PUT.\n"
                    f"       Run `stint revision --autogenerate` and `stint upgrade` "
                    f"with the current schema (default_level=None clears it).",
                    file=sys.stderr,
                )
                return 1
            print(f"\nsession.get({created_keys[0]!r}, fresh=True):")
            print(f"  summary:         {first.summary!r}")
            print(f"  severity:        {first.severity!r}")
            print(f"  reporter:        {first.reporter!r}")
            print(f"  test_text_field: {first.test_text_field!r}")

            # ── READ: filtered query (JQL under the hood) ────────────
            # select(Bug).where(Bug.c.severity == "S1") compiles to
            # cf[<id>] = "S1" — try stint's JQL view with stmt.compile(state).
            s1_bugs = await session.scalars(
                select(schema.Bug).where(schema.Bug.c.severity == "S1").order_by(schema.Bug.c.created, "DESC").limit(10)
            )
            print(f"\nseverity == 'S1' -> {len(s1_bugs)} hit(s) [S1]:")
            for hit in s1_bugs:
                print(f"  {hit.key}: {hit.summary!r}")

            either = await session.scalars(
                select(schema.Bug).where(or_(schema.Bug.c.severity == "S1", schema.Bug.c.severity == "S2"))
            )
            print(f"severity in (S1, S2) -> {len(either)} hit(s):")
            for hit in either:
                print(f"  {hit.key}: severity={hit.severity!r}")

            # ── WRITE: dirty-tracked update ───────────────────────────
            if bug_a.key:
                bug_a.severity = "S2"
                await session.commit()  # PUT only the changed field(s)
                print(f"\nupdated {bug_a.key}: severity -> 'S2'")
                reread = await session.get(schema.Bug, bug_a.key)
                if reread is None:
                    raise RuntimeError(f"PUT succeeded but GET {(bug_a.key or '')!r} returned nothing")
                print(f"re-read severity: {reread.severity!r}")

            # ── WRITE: delete (unless --keep) ─────────────────────────
            if not keep:
                for key in created_keys:
                    issue = await session.get(schema.Bug, key)
                    if issue is not None:
                        session.delete(issue)
                await session.commit()
                print(f"\ndeleted {len(created_keys)} demo issue(s)")
            else:
                print(f"\nkept {len(created_keys)} demo issue(s) in Jira (--keep)")
    finally:
        await engine.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SEC project: create issues, then read them back through the ORM.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the created issues in Jira instead of cleaning up",
    )
    parser.add_argument("--state", default="examples/company_managed/state/devel.yaml")
    parser.add_argument("--env", default="devel")
    parser.add_argument(
        "--schema",
        default=None,
        help="schema module path (defaults to the sibling security_scheme.py)",
    )
    args = parser.parse_args()
    state_path = args.state
    if not os.path.exists(state_path):
        print(
            f"ERROR: no state file at {state_path!s}. Run the migration path "
            f"first (see README.md), or pass --state <path>.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(
        asyncio.run(run(keep=args.keep, state_path=state_path, env_name=args.env, schema_target=args.schema))
    )


if __name__ == "__main__":
    main()
