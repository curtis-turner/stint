#!/usr/bin/env python3
"""Live-tenant smoke test for the typed duedate/parent system fields (#45).

Everything in tests/ and the other tools/ scripts is respx-mocked; this is
the pre-release check that a real tenant agrees with the mocked shapes.
It only creates and deletes its own issues in one sandbox project — no
schema-plane writes, nothing else on the tenant is touched.

Prerequisites:
  - A company-managed sandbox project whose issue types include a plain
    task type (default "Task") and a sub-task type (default "Sub-task").
  - Credentials: STINT_TOKEN (+ STINT_USER for api-token auth).

Run it:

    STINT_USER='you@example.com' STINT_TOKEN='...' \
    uv run python tools/smoke_dataplane.py \
        --url https://you.atlassian.net --project-key SANDBOX

    # keep the created issues in Jira instead of deleting them:
    ... uv run python tools/smoke_dataplane.py --keep

Every step prints PASS/FAIL; the exit code is 0 only if all steps passed.
Created issues are deleted in a finally block unless --keep.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

# Running a script by file path puts the script's directory at sys.path[0].
# Nothing here needs it, and it can shadow stdlib modules (see
# examples/company_managed/create_and_query_issues.py for the attrs/cyclopts
# collision that motivated this).
_SELF_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if entry != str(_SELF_DIR)]

from stint import IssueType, StateFile, create_engine  # noqa: E402
from stint.cli.cmd_reflect import _build_auth  # noqa: E402
from stint.exceptions import ConfigurationError, NotFoundError  # noqa: E402
from stint.query import select  # noqa: E402
from stint.query.session import AsyncSession, PartialCommitError  # noqa: E402
from stint.state.file import ProjectMapping, SimpleMapping  # noqa: E402

# ── Smoke models ──────────────────────────────────────────────────────
# __alias__ matches the live issuetype *display name* (the script keys state
# by name.lower(), the same match `stint stamp` performs). Adjust --task-type
# / --subtask-type if the sandbox project's type names differ.


class SmokeTask(IssueType):
    __alias__ = "task"
    summary: str
    duedate: date | None = None


class SmokeSubtask(IssueType):
    __alias__ = "sub-task"
    __subtask__ = True
    summary: str
    duedate: date | None = None
    parent: str | None = None


# ── Harness ───────────────────────────────────────────────────────────
class Checks:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures += 1


async def build_state(engine, base_url: str, project_key: str) -> StateFile:
    """Reflect the tenant and build the minimal in-memory state the data
    plane needs: the target project plus global issue-type ids. No schema
    module, no state file — the same name-matching `stint stamp` does.

    Name-matching rules borrowed from the stamp algorithm
    (`stint/autogen/stamp.py`): project-scoped (team-managed) issue types are
    ignored — /issuetype returns them alongside global ones, several can
    share a display name (one per TMP project), and their ids are invalid
    for POST /issue elsewhere (observed live: "Specify a valid issue type").
    Ambiguous global names are refused rather than bound arbitrarily.
    """
    snapshot = await engine.dialect.reflect()
    state = StateFile(env="smoke", jira_url=base_url)

    projects = {p.key: p for p in snapshot.projects.values()}
    proj = projects.get(project_key)
    if proj is None:
        raise SystemExit(f"project {project_key!r} not found on the tenant (visible: {sorted(projects)})")
    if proj.style == "next-gen":
        raise SystemExit(
            f"project {project_key!r} is team-managed (next-gen); this smoke "
            f"exercises the CMP data plane — point it at a company-managed project."
        )
    state.projects[project_key] = ProjectMapping(id=proj.id, key=proj.key)

    globals_by_name: dict[str, list] = {}
    for it in snapshot.issuetypes.values():
        if it.project_scoped:
            continue
        globals_by_name.setdefault(it.name.lower(), []).append(it)

    # What the project can actually create (createmeta): a global type the
    # project's issue-type scheme doesn't carry would still 400 mid-smoke.
    meta = await engine.client.get_json(
        f"{engine.dialect.api_root}/issue/createmeta",
        params={"projectKeys": project_key},
    )
    creatable = {str(t.get("name", "")).lower() for p in meta.get("projects", []) for t in p.get("issuetypes", [])}

    for alias in (SmokeTask.__alias__, SmokeSubtask.__alias__):
        candidates = globals_by_name.get(alias.lower()) or []
        if len(candidates) > 1:
            raise SystemExit(
                f"issue type name {alias!r} matches multiple global issue types "
                f"({[c.id for c in candidates]}); refusing to guess. Rename the "
                f"sandbox type or pass --task-type / --subtask-type."
            )
        it = candidates[0] if candidates else None
        if it is None:
            raise SystemExit(
                f"issue type {alias!r} not found on the tenant (global types: "
                f"{sorted(globals_by_name)}). Pass --task-type / --subtask-type "
                f"to match the sandbox project's type names."
            )
        if alias.lower() not in creatable:
            raise SystemExit(
                f"issue type {alias!r} exists globally (id {it.id}) but is not "
                f"creatable on {project_key!r} (creatable: {sorted(creatable)}). "
                f"Add it to the project's issue-type scheme or pick another project."
            )
        print(f"state:   {alias!r} -> issuetype id {it.id}")
        state.issuetypes[alias] = SimpleMapping(id=it.id)
    return state


async def run(args: argparse.Namespace) -> int:
    checks = Checks()
    if not os.environ.get("STINT_TOKEN"):
        raise SystemExit("export STINT_TOKEN (and STINT_USER for api-token auth)")
    auth = _build_auth(args.auth, "STINT_TOKEN", "STINT_USER")
    engine = create_engine(args.url, auth=auth, verify_ssl=not args.no_verify_ssl)

    created: list[IssueType] = []

    def bail(label: str, detail: str) -> int:
        """Record a failed step and stop: later steps depend on earlier keys."""
        checks.check(label, False, detail)
        return 1

    async def commit(session: AsyncSession, *instances: IssueType) -> None:
        """Commit, recording every successful insert for cleanup even when
        the batch raises PartialCommitError (those instances exist in Jira)."""
        try:
            await session.commit()
        except PartialCommitError as exc:
            for result in exc.results:
                if result.success and result.instance.key:
                    created.append(result.instance)
            raise
        for instance in instances:
            if instance.key:
                created.append(instance)

    try:
        state = await build_state(engine, args.url, args.project_key)
        print(f"tenant:  {args.url}")
        print(f"project: {args.project_key} (company-managed)")

        try:
            async with AsyncSession(engine, state) as session:
                target = date.today()

                # 1. Insert a task carrying a duedate.
                task = SmokeTask(summary="smoke: duedate/parent parent", duedate=target)
                session.add(task, project=args.project_key)
                await commit(session, task)
                task_key = task.key
                if not task_key:
                    return bail("insert task with duedate", "no key assigned")
                checks.check("insert task with duedate", True, f"key={task_key}")

                # 2. Read it back: duedate hydrates to a date.
                reread = await session.get(SmokeTask, task_key, fresh=True)
                if reread is None:
                    return bail("duedate hydrates as date", f"GET {task_key} returned nothing")
                checks.check(
                    "duedate hydrates as date",
                    reread.duedate == target,
                    f"duedate={reread.duedate!r}",
                )

                # 3. Offline guard: a __subtask__ model without parent must
                # fail in the payload builder, before any request.
                guard_raised = False
                try:
                    parentless = SmokeSubtask(summary="smoke: parentless")
                    session.add(parentless, project=args.project_key)
                    await session.commit()
                    if parentless.key:  # only if Jira somehow accepted it
                        created.append(parentless)
                except ConfigurationError as e:
                    guard_raised = "parent" in str(e)
                    checks.check("parentless subtask refused client-side", guard_raised, str(e)[:100])
                except PartialCommitError:
                    guard_raised = False
                if not guard_raised:
                    return bail("parentless subtask refused client-side", "no ConfigurationError raised")

                # 4. Insert the sub-task under the task.
                sub = SmokeSubtask(summary="smoke: duedate/parent subtask", duedate=target, parent=task_key)
                session.add(sub, project=args.project_key)
                await commit(session, sub)
                sub_key = sub.key
                if not sub_key:
                    return bail("insert subtask with parent + duedate", "no key assigned")
                checks.check("insert subtask with parent + duedate", True, f"key={sub_key}")

                # 5. Read it back: parent hydrates as the parent's key.
                sub_reread = await session.get(SmokeSubtask, sub_key, fresh=True)
                if sub_reread is None:
                    return bail("parent hydrates as parent key", f"GET {sub_key} returned nothing")
                checks.check(
                    "parent hydrates as parent key",
                    sub_reread.parent == task_key,
                    f"parent={sub_reread.parent!r}",
                )

                # 6. Clear the duedate on update (dirty None -> JSON null).
                sub_reread.duedate = None
                await session.commit()
                cleared = await session.get(SmokeSubtask, sub_key, fresh=True)
                if cleared is None:
                    return bail("duedate cleared with null on update", f"GET {sub_key} returned nothing")
                checks.check(
                    "duedate cleared with null on update",
                    cleared.duedate is None,
                    f"duedate={cleared.duedate!r}",
                )

                # 7. Move the sub-task to a second parent.
                task_b = SmokeTask(summary="smoke: second parent")
                session.add(task_b, project=args.project_key)
                await commit(session, task_b)
                task_b_key = task_b.key
                if not task_b_key:
                    return bail("parent moved on update", "second task insert failed")
                moved = await session.get(SmokeSubtask, sub_key)
                if moved is None:
                    return bail("parent moved on update", f"GET {sub_key} returned nothing")
                moved.parent = task_b_key
                await session.commit()
                moved_back = await session.get(SmokeSubtask, sub_key, fresh=True)
                if moved_back is None:
                    return bail("parent moved on update", f"GET {sub_key} returned nothing")
                checks.check(
                    "parent moved on update",
                    moved_back.parent == task_b_key,
                    f"parent={moved_back.parent!r}",
                )

                # 8. JQL round-trip on duedate (system-field queryability).
                hits = await session.scalars(select(SmokeTask).where(SmokeTask.c.duedate == target))
                checks.check(
                    "duedate queryable in JQL",
                    any(h.key == task_key for h in hits),
                    f"{len(hits)} hit(s) for duedate == {target.isoformat()}",
                )
        finally:
            # Cleanup even on failure: delete everything this run created.
            if created and not args.keep:
                try:
                    async with AsyncSession(engine, state) as session:
                        deletable = [i for i in created if i.key]
                        for instance in deletable:
                            key = instance.key
                            if key is None:  # guarded above; defensive
                                continue
                            fresh = await session.get(type(instance), key)
                            if fresh is not None:
                                session.delete(fresh)
                        await session.commit()
                        print(f"cleanup: deleted {len(deletable)} smoke issue(s)")
                except Exception as e:  # noqa: BLE001 — cleanup is best-effort by design
                    keys = [i.key for i in created if i.key]
                    print(f"cleanup FAILED: {e}; delete manually: {keys}", file=sys.stderr)
            elif args.keep:
                print(f"kept {[i.key for i in created if i.key]} (--keep)")
    except (NotFoundError, ConfigurationError, PartialCommitError) as e:
        print(f"ABORT: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        await engine.close()

    print()
    if checks.failures:
        print(f"SMOKE FAILED: {checks.failures} check(s) failed")
        return 1
    print("SMOKE PASSED")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("SMOKE_JIRA_URL"), help="e.g. https://you.atlassian.net")
    parser.add_argument("--project-key", default=os.environ.get("SMOKE_PROJECT"), help="sandbox project key")
    parser.add_argument("--auth", choices=["api-token", "pat", "basic"], default="api-token")
    parser.add_argument("--task-type", default="task", help="display name of the parent issue type")
    parser.add_argument("--subtask-type", default="sub-task", help="display name of the sub-task issue type")
    parser.add_argument("--keep", action="store_true", help="keep created issues (default: delete)")
    parser.add_argument("--no-verify-ssl", action="store_true")
    args = parser.parse_args()

    if not args.url or not args.project_key:
        raise SystemExit("--url and --project-key are required (or SMOKE_JIRA_URL / SMOKE_PROJECT)")
    # Allow non-default type names without editing the module: the state
    # builder reads __alias__ at run time.
    SmokeTask.__alias__ = args.task_type
    SmokeSubtask.__alias__ = args.subtask_type
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
