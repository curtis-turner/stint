#!/usr/bin/env python3
"""Live-tenant smoke test for the typed duedate/parent system fields (#45).

Everything in tests/ and the other tools/ scripts is respx-mocked; this is
the pre-release check that a real tenant agrees with the mocked shapes.
It only creates and deletes its own issues in one sandbox project — no
schema-plane writes, nothing else on the tenant is touched.

Issue types are chosen from what the project can actually create
(/issue/createmeta with expand=projects.issuetypes.fields): the parent role
goes to a creatable non-subtask type (zero-required-fields types first),
the sub-task role to a creatable type with ``subtask: true``. Types whose
create screen requires custom fields are supported via
--required-field "Name=Value" (repeatable; values shaped from the reflect
snapshot's field type — select options become {"value": ...}, and so on).
Pass --task-type / --subtask-type to pin names. If the project has no
creatable sub-task type, --provision-subtask stands one up through
stint's dialect ops (create-or-reuse + scheme membership, persists after
the run); otherwise the parent checks are SKIPPED with a notice and only
the duedate checks run.

Prerequisites:
  - A company-managed sandbox project.
  - Credentials: STINT_TOKEN (+ STINT_USER for api-token auth).

Run it:

    STINT_USER='you@example.com' STINT_TOKEN='...' \
    uv run python tools/smoke_dataplane.py \
        --url https://you.atlassian.net --project-key SEC \
        --required-field 'Severity=S1'

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
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

# Running a script by file path puts the script's directory at sys.path[0].
# Nothing here needs it, and it can shadow stdlib modules (see
# examples/company_managed/create_and_query_issues.py for the attrs/cyclopts
# collision that motivated this).
_SELF_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if entry != str(_SELF_DIR)]

from stint import CustomField, IssueType, StateFile, create_engine  # noqa: E402
from stint.cli.cmd_reflect import _build_auth  # noqa: E402
from stint.exceptions import ConfigurationError, NotFoundError  # noqa: E402
from stint.fields import (  # noqa: E402
    CheckboxesField,
    DateField,
    DateTimeField,
    LabelsField,
    MultiSelectField,
    NumberField,
    RadioButtonsField,
    SelectField,
    TextAreaField,
    TextField,
    URLField,
)
from stint.query import select  # noqa: E402
from stint.query.session import AsyncSession, PartialCommitError  # noqa: E402
from stint.schema.issuetype import IssueTypeMeta  # noqa: E402
from stint.state.file import CustomFieldMapping, ProjectMapping, SimpleMapping  # noqa: E402
from stint.state.snapshot import CustomFieldSnapshot, IssueTypeSnapshot  # noqa: E402


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
class RequiredField:
    """One required-on-create custom field of the chosen issue type, with
    the raw value supplied via --required-field."""

    field_id: str
    name: str
    raw: str
    snapshot: CustomFieldSnapshot

    def attr(self, index: int) -> str:
        return f"req_{index}"


# Reflect's customfieldtypes suffixes → (stint field type, value kind).
# Kinds drive value parsing and (via the field type) the payload shape.
_FIELD_KINDS: dict[str, tuple[type, str]] = {
    "select": (SelectField, "single_option"),
    "radiobuttons": (RadioButtonsField, "single_option"),
    "multiselect": (MultiSelectField, "multi_option"),
    "checkboxes": (CheckboxesField, "multi_option"),
    "text": (TextField, "raw"),
    "textarea": (TextAreaField, "raw"),
    "url": (URLField, "raw"),
    "number": (NumberField, "number"),
    "date": (DateField, "raw"),
    "datetime": (DateTimeField, "raw"),
    "labels": (LabelsField, "raw_list"),
}


def _kind(rf: RequiredField) -> str:
    type_id = rf.snapshot.type_id.rsplit(":", 1)[-1]
    mapping = _FIELD_KINDS.get(type_id)
    if mapping is None:
        raise SystemExit(
            f"--required-field {rf.name!r}: field type {rf.snapshot.type_id!r} is "
            f"not supported by this smoke (needs accountId/name resolution or is "
            f"unknown). Run against a project/type that doesn't require it."
        )
    return mapping[1]


def _parse_value(rf: RequiredField) -> Any:
    """Parse the raw --required-field value per the field's kind, warning
    when a select-style value isn't among the reflected options."""
    kind = _kind(rf)
    options = list(rf.snapshot.options)
    if kind == "number":
        text = rf.raw.strip()
        try:
            return int(text)
        except ValueError:
            return float(text)
    if kind in ("multi_option", "raw_list"):
        values = [v.strip() for v in rf.raw.split(",") if v.strip()]
        if kind == "multi_option":
            for v in values:
                if options and v not in options:
                    print(f"  warn: {rf.name}={v!r} is not a reflected option ({options})", file=sys.stderr)
            return values
        return values
    if kind == "single_option" and options and rf.raw not in options:
        print(f"  warn: {rf.name}={rf.raw!r} is not a reflected option ({options})", file=sys.stderr)
    return rf.raw


def _annotation_for(rf: RequiredField) -> Any:
    """The dynamic attribute annotation: stint's field type inside Annotated,
    so the payload builder shapes the value ({\"value\": ...} for selects)."""
    type_id = rf.snapshot.type_id.rsplit(":", 1)[-1]
    field_type, kind = _FIELD_KINDS[type_id]
    options = list(rf.snapshot.options)
    # The annotation is assembled at runtime from reflected metadata — ty
    # cannot see these as static type expressions.
    cf = CustomField(alias=rf.field_id, name=rf.name, type=field_type, options=options)  # ty: ignore
    if kind == "single_option":
        if not options:
            raise SystemExit(
                f"--required-field {rf.name!r}: select-style field with no "
                f"reflected options; cannot declare it. Run against a project "
                f"that doesn't require it."
            )
        return Annotated[Literal[tuple(options)], cf]  # ty: ignore
    if kind == "multi_option":
        if not options:
            raise SystemExit(
                f"--required-field {rf.name!r}: multi-select-style field with no "
                f"reflected options; cannot declare it. Run against a project "
                f"that doesn't require it."
            )
        return Annotated[list[Literal[tuple(options)]], cf]  # ty: ignore
    return Annotated[field_type, cf]  # ty: ignore


def build_smoke_model(*, name: str, alias: str, subtask: bool, required: list[RequiredField]) -> type[IssueType]:
    """Build the issue-type model for one role, with the required custom
    fields as Annotated attributes resolved via state (alias = field id).

    Built through IssueTypeMeta directly (a dynamic class statement would
    need exec to keep the metaclass validations)."""
    annotations: dict[str, Any] = {"summary": str, "duedate": date | None}
    ns: dict[str, Any] = {"__alias__": alias, "summary": None, "duedate": None}
    if subtask:
        ns["__subtask__"] = True
        annotations["parent"] = str | None
        ns["parent"] = None
    for i, rf in enumerate(required):
        annotations[rf.attr(i)] = _annotation_for(rf)
        ns[rf.attr(i)] = None
    ns["__annotations__"] = annotations
    return IssueTypeMeta(name, (IssueType,), ns)  # type: ignore[return-value]


@dataclass
class Plan:
    """The issue-type names/models this smoke run will use per role.

    The models are built at runtime from reflected metadata (dynamic
    annotations), so their type is Any — attribute access on instances is
    checked by the offline probe and the live run, not by ty."""

    task: str
    task_model: Any
    task_required: list[RequiredField] = field(default_factory=list)
    subtask: str | None = None  # None -> parent checks are skipped
    subtask_model: Any = None
    subtask_required: list[RequiredField] = field(default_factory=list)


async def provision_subtask(engine, proj, snapshot, args: argparse.Namespace) -> tuple[str, str]:
    """Ensure a creatable global sub-task type is on the project's issue-type
    scheme, using stint's own dialect ops (no schema-plane migration, no UI).

    Creates the type only if no global type carries the name; scheme
    membership appends (reconcile removes nothing here — current ⊆ desired).
    The type and membership persist after the smoke: harmless on a sandbox
    and reusable by later runs; remove in the Jira UI if unwanted.
    """
    name = args.subtask_type or "Smoke Sub-task"
    existing = await engine.dialect.find_issuetype_id_by_name(name)
    if existing:
        print(f"provision: reusing global issue type {name!r} (id {existing})")
    else:
        existing = await engine.dialect.create_issuetype(
            name=name,
            description="Provisioned by tools/smoke_dataplane.py (--provision-subtask)",
            subtask=True,
        )
        print(f"provision: created global sub-task type {name!r} (id {existing})")

    scheme_id = proj.issuetype_scheme_id
    if not scheme_id:
        raise SystemExit(
            f"project {proj.key!r} uses the global default issue-type scheme; "
            f"adding a member would affect every project sharing it. Assign a "
            f"project-level issue-type scheme first, or add {name!r} in the UI."
        )
    scheme = snapshot.issuetype_schemes.get(scheme_id)
    members = list(scheme.issuetype_ids) if scheme else []
    if existing not in members:
        await engine.dialect.set_issuetype_scheme_issuetypes(scheme_id, issuetype_ids=[*members, existing])
        print(f"provision: added {name!r} to issue-type scheme {scheme_id!r}")
    else:
        print(f"provision: {name!r} is already on issue-type scheme {scheme_id!r}")
    return name, existing


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

    # What the project can actually create (createmeta with per-type fields),
    # with the subtask flag and required-on-create fields: the role source.
    # A global type the project's scheme doesn't carry would still 400
    # mid-smoke (observed live: "Specify a valid issue type"), and a required
    # custom field would too (observed live: "Severity is required.").
    # Identity/system fields stint's payload builder handles automatically
    # (project + issuetype injected from state, reporter stripped on insert)
    # are not the caller's problem — createmeta still lists them required.
    async def fetch_creatable() -> list[dict[str, Any]]:
        meta = await engine.client.get_json(
            f"{engine.dialect.api_root}/issue/createmeta",
            params={"projectKeys": project_key, "expand": "projects.issuetypes.fields"},
        )
        parsed: list[dict[str, Any]] = []
        for p in meta.get("projects", []):
            for t in p.get("issuetypes", []):
                required = [
                    (fid, str(f.get("name", fid)))
                    for fid, f in (t.get("fields") or {}).items()
                    if fid not in {"summary", "project", "issuetype", "reporter"} and f.get("required")
                ]
                parsed.append({"name": str(t.get("name", "")), "subtask": bool(t.get("subtask")), "required": required})
        return parsed

    creatable = await fetch_creatable()

    # No creatable sub-task type? --provision-subtask stands one up through
    # stint's own dialect ops: create (or reuse) a global subtask type, then
    # append it to the project's issue-type scheme. The type and its scheme
    # membership persist after the smoke — harmless on a sandbox, and
    # reusable by later runs.
    provisioned: tuple[str, str] | None = None  # (name, id)
    if args.provision_subtask and not any(c["subtask"] for c in creatable):
        provisioned = await provision_subtask(engine, proj, snapshot, args)
        globals_by_name.setdefault(provisioned[0].lower(), []).append(
            IssueTypeSnapshot(id=provisioned[1], name=provisioned[0])
        )
        creatable = await fetch_creatable()

    def resolve(role: str, pinned: str | None, want_subtask: bool) -> dict[str, Any]:
        """Pick the createmeta entry for a role: the pinned type, else the
        first creatable match with the fewest required fields (zero first)."""
        pool = [c for c in creatable if c["subtask"] == want_subtask]
        if pinned:
            matches = [c for c in pool if c["name"].lower() == pinned.lower()]
            if not matches:
                raise SystemExit(
                    f"--{role}-type {pinned!r} is not creatable on {project_key!r} "
                    f"(creatable: {[c['name'] for c in creatable]})."
                )
            return matches[0]
        if not pool:
            return {}
        return sorted(pool, key=lambda c: (len(c["required"]), c["name"]))[0]

    task_entry = resolve("task", args.task_type, want_subtask=False)
    if not task_entry:
        raise SystemExit(
            f"no creatable non-subtask issue type on {project_key!r} "
            f"(creatable: {[c['name'] for c in creatable]}); nothing to smoke."
        )
    # A provisioned name is guaranteed creatable — pin it so resolve() can't
    # be thrown off by ordering.
    subtask_pinned = args.subtask_type or (provisioned[0] if provisioned else None)
    subtask_entry = resolve("subtask", subtask_pinned, want_subtask=True)

    # --required-field values by field name (case-insensitive). Both roles
    # may require the same field (e.g. Severity on Bug and on sub-tasks), so
    # lookups never pop; leftovers (typos) are reported after both binds.
    provided = {k.lower(): v for k, v in (a.split("=", 1) for a in args.required_field)}
    consumed: set[str] = set()

    def bind_required(entry: dict[str, Any]) -> list[RequiredField]:
        fields: list[RequiredField] = []
        for field_id, field_name in entry.get("required", []):
            key = field_name.lower()
            raw = provided.get(key)
            if raw is None:
                raise SystemExit(
                    f"issue type {entry['name']!r} requires {field_name!r} "
                    f"({field_id}) on create. Pass --required-field "
                    f'"{field_name}=<value>" (repeatable).'
                )
            consumed.add(key)
            snap = snapshot.custom_fields.get(field_id)
            if snap is None:
                raise SystemExit(
                    f"required field {field_name!r} ({field_id}) is not in the "
                    f"reflect snapshot's custom fields; cannot shape its value."
                )
            fields.append(RequiredField(field_id=field_id, name=field_name, raw=raw, snapshot=snap))
        return fields

    task_required = bind_required(task_entry)
    subtask_required = bind_required(subtask_entry) if subtask_entry else []
    leftover = sorted(set(provided) - consumed)
    if leftover:
        print(f"  warn: unused --required-field value(s): {leftover}", file=sys.stderr)

    # Bind ids from the global snapshot. Every creatable CMP type must exist
    # globally; a mismatch means createmeta and /issuetype disagree, which is
    # itself worth refusing loudly.
    def bind_type(entry: dict[str, Any]) -> str:
        name = entry["name"]
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
        required_names = [f[1] for f in entry["required"]] or "none"
        print(f"state:   {name!r} -> issuetype id {candidates[0].id} (required: {required_names})")
        state.issuetypes[name] = SimpleMapping(id=candidates[0].id)
        return name

    task_name = bind_type(task_entry)
    subtask_name = bind_type(subtask_entry) if subtask_entry else None

    # Required custom fields resolve through state by alias (= field id).
    for rf in [*task_required, *subtask_required]:
        state.custom_fields[rf.field_id] = CustomFieldMapping(id=rf.field_id)

    task_model = build_smoke_model(name="SmokeTaskModel", alias=task_name, subtask=False, required=task_required)
    subtask_model = (
        build_smoke_model(name="SmokeSubtaskModel", alias=subtask_name, subtask=True, required=subtask_required)
        if subtask_name
        else None
    )
    return state, Plan(
        task=task_name,
        task_model=task_model,
        task_required=task_required,
        subtask=subtask_name,
        subtask_model=subtask_model,
        subtask_required=subtask_required,
    )


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
        print(f"tenant:  {args.url}")
        print(f"project: {args.project_key} (company-managed)")

        def kwargs_for(required: list[RequiredField]) -> dict[str, Any]:
            """Constructor kwargs for the required custom fields."""
            return {rf.attr(i): _parse_value(rf) for i, rf in enumerate(required)}

        try:
            async with AsyncSession(engine, state) as session:
                target = date.today()

                # ── duedate group (needs any creatable non-subtask type) ──
                # 1. Insert carrying a duedate.
                task = plan.task_model(
                    summary="smoke: duedate/parent parent", duedate=target, **kwargs_for(plan.task_required)
                )
                session.add(task, project=args.project_key)
                await commit(session, task)
                task_key = task.key
                if not task_key:
                    return bail("insert with duedate", "no key assigned")
                checks.check(f"insert {plan.task} with duedate", True, f"key={task_key}")

                # 2. Read it back: duedate hydrates to a date.
                reread = await session.get(type(task), task_key, fresh=True)
                if reread is None:
                    return bail("duedate hydrates as date", f"GET {task_key} returned nothing")
                checks.check(
                    "duedate hydrates as date",
                    reread.duedate == target,
                    f"duedate={reread.duedate!r}",
                )

                # 3. JQL round-trip on duedate (system-field queryability).
                # Cloud's search index is eventually consistent: a
                # just-created issue can be missing from JQL results for a
                # few seconds, so poll with a deadline instead of one shot.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 20.0
                found = False
                hits: list = []
                while True:
                    hits = await session.scalars(
                        select(type(task)).where(type(task).c.duedate == target)  # type: ignore[attr-defined]
                    )
                    if any(h.key == task_key for h in hits):
                        found = True
                        break
                    if loop.time() > deadline:
                        break
                    await asyncio.sleep(2.0)
                checks.check(
                    "duedate queryable in JQL",
                    found,
                    f"{len(hits)} hit(s) for duedate == {target.isoformat()}"
                    + ("" if found else " (still absent after 20s — index lag or a real JQL gap)"),
                )

                if plan.subtask_model is None:
                    checks.skip(
                        "parent group (guard, insert, hydrate, move)",
                        f"no sub-task type is creatable on {args.project_key!r} — "
                        f"run against a project whose scheme carries a sub-task "
                        f"type to cover #45's parent half",
                    )
                    # 4. duedate clear on update, on the task itself.
                    reread.duedate = None
                    await session.commit()
                    cleared = await session.get(type(task), task_key, fresh=True)
                    if cleared is None:
                        return bail("duedate cleared with null on update", f"GET {task_key} returned nothing")
                    checks.check(
                        "duedate cleared with null on update",
                        cleared.duedate is None,
                        f"duedate={cleared.duedate!r}",
                    )
                    print()
                    if checks.failures:
                        print(f"SMOKE FAILED: {checks.failures} check(s) failed (parent group not run)")
                        return 1
                    print(f"SMOKE PASSED with {checks.skips} skipped group(s) — duedate verified, parent not")
                    return 0

                # ── parent group (needs a creatable sub-task type) ────────
                subtask_kwargs = kwargs_for(plan.subtask_required)

                # 5. Offline guard: a __subtask__ model without parent must
                # fail in the payload builder, before any request.
                guard_raised = False
                try:
                    parentless = plan.subtask_model(summary="smoke: parentless", **subtask_kwargs)
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
                sub = plan.subtask_model(
                    summary="smoke: duedate/parent subtask", duedate=target, parent=task_key, **subtask_kwargs
                )
                session.add(sub, project=args.project_key)
                await commit(session, sub)
                sub_key = sub.key
                if not sub_key:
                    return bail("insert subtask with parent + duedate", "no key assigned")
                checks.check(f"insert {plan.subtask} with parent + duedate", True, f"key={sub_key}")

                # 7. Read it back: parent hydrates as the parent's key.
                sub_reread = await session.get(type(sub), sub_key, fresh=True)
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
                cleared = await session.get(type(sub), sub_key, fresh=True)
                if cleared is None:
                    return bail("duedate cleared with null on update", f"GET {sub_key} returned nothing")
                checks.check(
                    "duedate cleared with null on update",
                    cleared.duedate is None,
                    f"duedate={cleared.duedate!r}",
                )

                # 9. Move the sub-task to a second parent.
                task_b = plan.task_model(summary="smoke: second parent", **kwargs_for(plan.task_required))
                session.add(task_b, project=args.project_key)
                await commit(session, task_b)
                task_b_key = task_b.key
                if not task_b_key:
                    return bail("parent moved on update", "second insert failed")
                moved = await session.get(type(sub), sub_key)
                if moved is None:
                    return bail("parent moved on update", f"GET {sub_key} returned nothing")
                moved.parent = task_b_key
                await session.commit()
                moved_back = await session.get(type(sub), sub_key, fresh=True)
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
        "--task-type",
        default=None,
        help="pin the parent-role type name (default: fewest-required creatable non-subtask)",
    )
    parser.add_argument(
        "--subtask-type",
        default=None,
        help="pin the sub-task role name (default: first creatable subtask)",
    )
    parser.add_argument(
        "--provision-subtask",
        action="store_true",
        help="no creatable sub-task type? create (or reuse) a global sub-task "
        "type and append it to the project's issue-type scheme. Persists "
        "after the run.",
    )
    parser.add_argument(
        "--required-field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="required-on-create custom field value (repeatable)",
    )
    parser.add_argument("--keep", action="store_true", help="keep created issues (default: delete)")
    parser.add_argument("--no-verify-ssl", action="store_true")
    args = parser.parse_args()

    if not args.url or not args.project_key:
        raise SystemExit("--url and --project-key are required (or SMOKE_JIRA_URL / SMOKE_PROJECT)")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
