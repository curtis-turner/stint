#!/usr/bin/env python3
"""Live-tenant smoke test for the typed duedate/parent system fields (#45).

Everything in tests/ and the other tools/ scripts is respx-mocked; this is
the pre-release check that a real tenant agrees with the mocked shapes.
It only creates and deletes its own issues in one sandbox project — no
schema-plane writes, nothing else on the tenant is touched.

Issue types are chosen from what the project can actually create
(/issue/createmeta): the parent role goes to the first creatable
non-subtask type, the sub-task role to the first creatable type with
``subtask: true``. Pass --task-type / --subtask-type to pin names. If the
project has no creatable sub-task type, the parent checks are SKIPPED with
a notice and only the duedate checks run.

Prerequisites:
  - A company-managed sandbox project.
  - Credentials: STINT_TOKEN (+ STINT_USER for api-token auth).

Run it:

    STINT_USER='you@example.com' STINT_TOKEN='...' \
    uv run python tools/smoke_dataplane.py \
        --url https://you.atlassian.net --project-key SANDBOX

    # keep the created issues in Jira instead of deleting them:
    ... uv run python tools/smoke_dataplane.py --keep

Every step prints PASS/FAIL/SKIP; the exit code is 0 only if no step failed.
Created issues are deleted in a finally block unless --keep.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
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

# Smoke models: __alias__ is bound at run time to the issue-type names the
# pre-flight chose (payload building reads __alias__ per commit).


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
        self.skips = 0

    def skip(self, label: str, why: str) -> None:
        self.skips += 1
        print(f"  [SKIP] {label} — {why}")

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures += 1


@dataclass
class Plan:
    """The issue-type names this smoke run will bind each role to."""

    task: str  # non-subtask type; always present (the run aborts otherwise)
    subtask: str | None  # subtask type; None -> parent checks are skipped


async def build_state(engine, base_url: str, project_key: str, args: argparse.Namespace) -> tuple[StateFile, Plan]:
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

    # Global (non-project-scoped) issue types by name: the id source.
    globals_by_name: dict[str, list] = {}
    for it in snapshot.issuetypes.values():
        if it.project_scoped:
            continue
        globals_by_name.setdefault(it.name.lower(), []).append(it)

    # What the project can actually create (createmeta), with the subtask
    # flag: the role source. A global type the project's issue-type scheme
    # doesn't carry would still 400 mid-smoke (observed live).
    meta = await engine.client.get_json(
        f"{engine.dialect.api_root}/issue/createmeta",
        params={"projectKeys": project_key},
    )
    creatable: list[tuple[str, bool]] = []
    for p in meta.get("projects", []):
        for t in p.get("issuetypes", []):
            creatable.append((str(t.get("name", "")), bool(t.get("subtask", False))))

    def resolve(role: str, pinned: str | None, want_subtask: bool) -> str | None:
        """Pick the type name for a role: the pinned one, or the first
        creatable match. Returns None when nothing qualifies (parent role
        only) — a missing task role is a hard abort at the callsite."""
        if pinned:
            matches = [name for name, subtask in creatable if name.lower() == pinned.lower()]
            if not matches:
                raise SystemExit(
                    f"--{role}-type {pinned!r} is not creatable on {project_key!r} "
                    f"(creatable: {sorted(n for n, _ in creatable)})."
                )
            return matches[0]
        matches = [name for name, subtask in creatable if subtask == want_subtask]
        return matches[0] if matches else None

    task_name = resolve("task", args.task_type, want_subtask=False)
    if task_name is None:
        raise SystemExit(
            f"no creatable non-subtask issue type on {project_key!r} "
            f"(creatable: {sorted(n for n, _ in creatable)}); nothing to smoke."
        )
    subtask_name = resolve("subtask", args.subtask_type, want_subtask=True)

    # Bind ids from the global snapshot. Every creatable CMP type must exist
    # globally; a mismatch means the tenant's createmeta and /issuetype
    # disagree, which is itself worth refusing loudly.
    for role, name in (("task", task_name), ("subtask", subtask_name)):
        if name is None:
            continue
        candidates = globals_by_name.get(name.lower()) or []
        if len(candidates) > 1:
            raise SystemExit(
                f"issue type name {name!r} matches multiple global issue types "
                f"({[c.id for c in candidates]}); refusing to guess. Rename the "
                f"sandbox type or pass --task-type / --subtask-type."
            )
        if not candidates:
            raise SystemExit(
                f"issue type {name!r} is creatable on {project_key!r} but not in "
                f"the global /issuetype list (global types: {sorted(globals_by_name)})."
            )
        print(f"state:   {role} role -> {name!r} (issuetype id {candidates[0].id})")
        state.issuetypes[name] = SimpleMapping(id=candidates[0].id)
    return state, Plan(task=task_name, subtask=subtask_name)


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
        state, plan = await build_state(engine, args.url, args.project_key, args)
        SmokeTask.__alias__ = plan.task
        SmokeSubtask.__alias__ = plan.subtask or "sub-task"
        print(f"tenant:  {args.url}")
        print(f"project: {args.project_key} (company-managed)")

        try:
            async with AsyncSession(engine, state) as session:
                target = date.today()

                # ── duedate group (needs any creatable non-subtask type) ──
                # 1. Insert carrying a duedate.
                task = SmokeTask(summary="smoke: duedate/parent parent", duedate=target)
                session.add(task, project=args.project_key)
                await commit(session, task)
                task_key = task.key
                if not task_key:
                    return bail("insert with duedate", "no key assigned")
                checks.check(f"insert {plan.task} with duedate", True, f"key={task_key}")

                # 2. Read it back: duedate hydrates to a date.
                reread = await session.get(SmokeTask, task_key, fresh=True)
                if reread is None:
                    return bail("duedate hydrates as date", f"GET {task_key} returned nothing")
                checks.check(
                    "duedate hydrates as date",
                    reread.duedate == target,
                    f"duedate={reread.duedate!r}",
                )

                # 3. JQL round-trip on duedate (system-field queryability).
                hits = await session.scalars(select(SmokeTask).where(SmokeTask.c.duedate == target))
                checks.check(
                    "duedate queryable in JQL",
                    any(h.key == task_key for h in hits),
                    f"{len(hits)} hit(s) for duedate == {target.isoformat()}",
                )

                if plan.subtask is None:
                    checks.skip(
                        "parent group (guard, insert, hydrate, move)",
                        f"no sub-task type is creatable on {args.project_key!r} "
                        f"(creatable: non-subtask only) — run against a project "
                        f"whose scheme carries a sub-task type to cover #45's "
                        f"parent half",
                    )
                    # 4. duedate clear on update, on the task itself.
                    reread.duedate = None
                    await session.commit()
                    cleared = await session.get(SmokeTask, task_key, fresh=True)
                    if cleared is None:
                        return bail("duedate cleared with null on update", f"GET {task_key} returned nothing")
                    checks.check(
                        "duedate cleared with null on update",
                        cleared.duedate is None,
                        f"duedate={cleared.duedate!r}",
                    )
                    print()
                    print(f"SMOKE PASSED with {checks.skips} skipped group(s) — duedate verified, parent not")
                    return 0

                # ── parent group (needs a creatable sub-task type) ────────
                # 5. Offline guard: a __subtask__ model without parent must
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

                # 6. Insert the sub-task under the task.
                sub = SmokeSubtask(summary="smoke: duedate/parent subtask", duedate=target, parent=task_key)
                session.add(sub, project=args.project_key)
                await commit(session, sub)
                sub_key = sub.key
                if not sub_key:
                    return bail("insert subtask with parent + duedate", "no key assigned")
                checks.check(f"insert {plan.subtask} with parent + duedate", True, f"key={sub_key}")

                # 7. Read it back: parent hydrates as the parent's key.
                sub_reread = await session.get(SmokeSubtask, sub_key, fresh=True)
                if sub_reread is None:
                    return bail("parent hydrates as parent key", f"GET {sub_key} returned nothing")
                checks.check(
                    "parent hydrates as parent key",
                    sub_reread.parent == task_key,
                    f"parent={sub_reread.parent!r}",
                )

                # 8. Clear the duedate on update (dirty None -> JSON null).
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

                # 9. Move the sub-task to a second parent.
                task_b = SmokeTask(summary="smoke: second parent")
                session.add(task_b, project=args.project_key)
                await commit(session, task_b)
                task_b_key = task_b.key
                if not task_b_key:
                    return bail("parent moved on update", "second insert failed")
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
    parser.add_argument(
        "--task-type", default=None, help="pin the parent-role type name (default: first creatable non-subtask)"
    )
    parser.add_argument(
        "--subtask-type", default=None, help="pin the sub-task role name (default: first creatable subtask)"
    )
    parser.add_argument("--keep", action="store_true", help="keep created issues (default: delete)")
    parser.add_argument("--no-verify-ssl", action="store_true")
    args = parser.parse_args()

    if not args.url or not args.project_key:
        raise SystemExit("--url and --project-key are required (or SMOKE_JIRA_URL / SMOKE_PROJECT)")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
