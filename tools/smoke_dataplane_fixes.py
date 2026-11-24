#!/usr/bin/env python3
"""Live-tenant smoke test for the four data-plane fixes shipped in v0.18.1.

Exercises the fixes from DATA_PLANE_BUGS_ISSUE.md (found exercising 0.12-0.18
live) against a real Jira Cloud tenant. Everything in tests/ is respx-mocked;
this is the pre-release check that the live tenant agrees with the mocked
shapes. It only creates and deletes its own issues in one sandbox project —
no schema-plane writes, nothing else on the tenant is touched.

Groups (one per fix):
  1. createmeta expand — `dialect.get_createmeta` answers per-issuetype
     create-screen fields (the old `expand=fields` embedded none, so
     `engine.check` reported every field "not on the create screen"), and
     `engine.check()` accepts a model declaring those fields.
  2. link direction — `link(A, B, "Blocks")` stores "A blocks B": `links(A)`
     reads outward/B, `links(B)` inward/A; re-linking is a single-POST
     no-op; the reverse pair coexists with it.
  3. textarea ADF — a declared TextAreaField custom field survives insert +
     update + hydrate as plain text on Cloud (a bare string 400s there with
     "Operation value must be an Atlassian Document").
  4. changelog aliases — the textarea's changelog items map fieldId back to
     the declared alias ("smoke_notes"), not the display name Cloud reports
     in `field`.

Groups 3 and 4 need a multi-line text custom field on the chosen issue
type's create screen; the first one discovered is used (or pin one with
--textarea-field NAME). When the project has none, --provision-textarea
creates (or reuses by --textarea-name) a paragraph field and wires it onto
the project's screens and field configuration through stint's dialect ops;
without it the group is SKIPPED when the project has none.

Prerequisites:
  - A company-managed sandbox project on Jira Cloud.
  - Credentials: STINT_TOKEN (+ STINT_USER for api-token/basic auth).

Run it:

    STINT_USER='you@example.com' STINT_TOKEN='...' \
    uv run python tools/smoke_dataplane_fixes.py \
        --url https://you.atlassian.net --project-key PLAT

--url/--project-key fall back to SMOKE_JIRA_URL / JIRA_URL and
SMOKE_PROJECT / JIRA_PROJECT. Auth always comes from STINT_TOKEN plus
STINT_USER (api-token/basic) or STINT_TOKEN alone (pat).

Two full tenant reflects run (build_state plus engine.check's own), so
expect reflect-dominated runtime on large tenants. Every step prints
PASS/FAIL/SKIP; the exit code is 0 only if no step failed. Created issues
are deleted in a finally block unless --keep.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

# Running a script by file path puts the script's directory at sys.path[0].
# Nothing here needs it, and it can shadow stdlib modules (see
# examples/company_managed/create_and_query_issues.py for the attrs/cyclopts
# collision that motivated this).
_SELF_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if entry != str(_SELF_DIR)]

from stint import CustomField, StateFile, create_engine  # noqa: E402
from stint.cli.cmd_reflect import _build_auth  # noqa: E402
from stint.exceptions import ConfigurationError, NotFoundError, PartialCommitError  # noqa: E402
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
from stint.query.session import AsyncSession  # noqa: E402
from stint.schema.issuetype import IssueType, IssueTypeMeta  # noqa: E402
from stint.schema.project import Project, ProjectMeta  # noqa: E402
from stint.state.file import CustomFieldMapping, ProjectMapping, SimpleMapping  # noqa: E402

TEXTAREA_ALIAS = "smoke_notes"  # distinct from the field id, so fix 4's mapping is observable


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
    """One bound custom field: the reflect snapshot shapes its annotation
    and parses its value; ``field_id`` doubles as the state alias (identity
    mapping) except for the textarea, which binds under TEXTAREA_ALIAS so
    the changelog mapping has something to prove."""

    field_id: str
    name: str
    raw: str
    snapshot: Any  # CustomFieldSnapshot


# Reflect's customfieldtypes suffixes → (stint field type, value kind).
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


def _field_type(rf: RequiredField) -> tuple[type, str]:
    type_id = rf.snapshot.type_id.rsplit(":", 1)[-1]
    mapping = _FIELD_KINDS.get(type_id)
    if mapping is None:
        raise SystemExit(
            f"field {rf.name!r} ({rf.field_id}): type {rf.snapshot.type_id!r} is not supported "
            f"by this smoke (accountId/name resolution needed or unknown). "
            f"Run against a project/type that doesn't require it."
        )
    return mapping


def _parse_value(rf: RequiredField) -> Any:
    kind = _field_type(rf)[1]
    options = list(rf.snapshot.options)
    if kind == "number":
        text = rf.raw.strip()
        try:
            return int(text)
        except ValueError:
            return float(text)
    if kind in ("multi_option", "raw_list"):
        return [v.strip() for v in rf.raw.split(",") if v.strip()]
    if kind == "single_option" and options and rf.raw not in options:
        print(f"  warn: {rf.name}={rf.raw!r} is not a reflected option ({options})", file=sys.stderr)
    return rf.raw


def _annotation_for(rf: RequiredField, alias: str) -> Any:
    """Runtime attribute annotation for one bound custom field.

    The annotation is the VALUE type (str, Literal[...], ...) with the
    CustomField beside it, exactly as a declared schema writes it. Annotating
    with stint's marker class itself (Annotated[TextAreaField, cf]) raises
    PydanticSchemaGenerationError — those classes are not pydantic schemas.
    """
    field_type, kind = _field_type(rf)
    options = list(rf.snapshot.options)
    cf = CustomField(alias=alias, name=rf.name, type=field_type, options=options)  # ty: ignore
    if kind == "single_option":
        if not options:
            raise SystemExit(f"field {rf.name!r}: select-style field with no reflected options; cannot declare it.")
        return Annotated[Literal[tuple(options)] | None, cf]  # ty: ignore
    if kind == "multi_option":
        if not options:
            raise SystemExit(
                f"field {rf.name!r}: multi-select-style field with no reflected options; cannot declare it."
            )
        return Annotated[list[Literal[tuple(options)]] | None, cf]  # ty: ignore
    if kind == "raw_list":
        return Annotated[list[str] | None, cf]
    if kind == "number":
        return Annotated[int | float | None, cf]
    return Annotated[str | None, cf]


def build_model(
    *, name: str, alias: str, title: str, required: list[RequiredField], textarea: RequiredField | None
) -> type[IssueType]:
    """The issue-type model: summary + the required-on-create custom fields
    (+ the textarea under its smoke alias when discovered). Built through
    IssueTypeMeta directly — a dynamic class statement would need exec to
    keep the metaclass validations."""
    annotations: dict[str, Any] = {"summary": str}
    ns: dict[str, Any] = {"__alias__": alias, "__title__": title, "summary": None}
    for i, rf in enumerate(required):
        annotations[f"req_{i}"] = _annotation_for(rf, rf.field_id)
        ns[f"req_{i}"] = None
    if textarea is not None:
        annotations["notes"] = _annotation_for(textarea, TEXTAREA_ALIAS)
        ns["notes"] = None
    ns["__annotations__"] = annotations
    return IssueTypeMeta(name, (IssueType,), ns)  # type: ignore[return-value]


def build_check_project(model: Any, project_key: str) -> Any:
    """Minimal Project for engine.check: just the smoke's issue type."""
    ns: dict[str, Any] = {"__key__": project_key, "__issuetypes__": [model]}
    return ProjectMeta("SmokeFixesProject", (Project,), ns)


async def provision_textarea(
    engine: Any, proj: Any, snapshot: Any, issuetype_id: str, args: argparse.Namespace
) -> tuple[str, str]:
    """Create (or reuse) a paragraph custom field and wire it onto the
    project's create/edit screens and field configuration through stint's
    own dialect ops — the schema plane the data plane assumes.

    Reuses an existing textarea-typed field by name. Everything persists
    after the run (harmless on a sandbox, reusable by later runs); remove
    in the Jira UI if unwanted. Returns (field_id, display name).
    """
    name = args.textarea_name
    existing = next(
        (
            cf
            for cf in snapshot.custom_fields.values()
            if cf.name.lower() == name.lower() and cf.type_id.rsplit(":", 1)[-1] == "textarea"
        ),
        None,
    )
    if existing is not None:
        fid = existing.id
        print(f"provision: reusing textarea custom field {name!r} ({fid})")
    else:
        fid = await engine.dialect.create_custom_field(
            name=name,
            description="Provisioned by tools/smoke_dataplane_fixes.py (--provision-textarea)",
            type_id=TextAreaField.jira_type_id,
            searcher_key=TextAreaField.searcher_key,
        )
        print(f"provision: created paragraph custom field {name!r} ({fid})")

    # Project -> issue-type screen scheme -> screen scheme -> create/edit screens.
    itss = snapshot.issuetype_screen_schemes.get(proj.issuetype_screen_scheme_id or "")
    if itss is None:
        raise SystemExit(
            f"project {proj.key!r} uses the global default issue-type screen scheme; adding the "
            f"field would affect every project sharing it. Assign a project-level issue-type "
            f"screen scheme first, or add {name!r} to the screen in the UI."
        )
    screen_of_type = next((m for m in itss.mappings if m.issuetype_id == issuetype_id), None) or next(
        (m for m in itss.mappings if m.issuetype_id == "default"), None
    )
    if screen_of_type is None:
        raise SystemExit(f"issue-type screen scheme {itss.id!r} maps neither {issuetype_id} nor 'default'.")
    scheme = snapshot.screen_schemes.get(screen_of_type.screen_scheme_id)
    if scheme is None:
        raise SystemExit(f"screen scheme {screen_of_type.screen_scheme_id!r} is not in the reflect snapshot.")
    create_screen = scheme.mappings.get("create") or scheme.mappings.get("default")
    edit_screen = scheme.mappings.get("edit") or scheme.mappings.get("default")
    if not create_screen:
        raise SystemExit(f"screen scheme {scheme.id!r} maps no create/default screen; cannot place the field.")

    for label, screen_id in (("create", create_screen), ("edit", edit_screen)):
        if not screen_id or (label == "edit" and screen_id == create_screen):
            continue
        screen = snapshot.screens.get(screen_id)
        if screen is None or not screen.tabs:
            print(
                f"provision:   warn: {label} screen {screen_id!r} has no reflected tabs; add {name!r} to it in the UI",
                file=sys.stderr,
            )
            continue
        tab = next((t for t in screen.tabs if t.name == "Field Tab"), screen.tabs[0])
        await engine.dialect.add_screen_tab_field(screen_id, tab.id, field_id=fid)
        print(f"provision: added {name!r} to the {label} screen {screen.name!r} (tab {tab.name!r})")

    # Field configuration (v1) or field scheme (v2) — set_field_configuration_item
    # routes by what the tenant answers; hidden=False associates on v2.
    if not proj.field_configuration_scheme_id:
        print(
            "provision:   note: project uses the global default field configuration "
            "(Jira includes new fields there) — skipping the field-configuration write"
        )
    else:
        fcs = snapshot.field_configuration_schemes.get(proj.field_configuration_scheme_id)
        if fcs is None:
            raise SystemExit(
                f"field configuration scheme {proj.field_configuration_scheme_id!r} is not in the reflect snapshot."
            )
        fc_of_type = next((m for m in fcs.mappings if m.issuetype_id == issuetype_id), None) or next(
            (m for m in fcs.mappings if m.issuetype_id == "default"), None
        )
        if fc_of_type is None:
            print(
                f"provision:   note: field configuration scheme {fcs.id!r} maps neither "
                f"{issuetype_id} nor 'default'; skipping the field-configuration write",
                file=sys.stderr,
            )
        else:
            await engine.dialect.set_field_configuration_item(
                fc_of_type.field_configuration_id,
                field_id=fid,
                required=False,
                hidden=False,
                description="Smoke notes (provisioned)",
            )
            print(f"provision: added {name!r} to field configuration {fc_of_type.field_configuration_id!r}")
    return fid, name


@dataclass
class Plan:
    """What this run will use: the issue type, its model, the bound required
    fields (constructor kwargs ready) and the textarea binding, if any."""

    task: str
    model: Any
    kwargs: dict[str, Any]
    textarea: RequiredField | None = None
    textarea_fid: str | None = None
    textarea_hint: str | None = None  # where a sibling type carries one


def find_textarea(entry: dict[str, Any], pinned: str | None) -> tuple[str, str] | None:
    """(field_id, display name) of a multi-line text CUSTOM field on the
    create screen. System textareas (description) are excluded — the system
    description was already ADF-wrapped before the fix."""
    found: list[tuple[str, str]] = []
    for fid, f in (entry.get("fields") or {}).items():
        if not str(fid).startswith("customfield_"):
            continue
        schema = f.get("schema") or {}
        if str(schema.get("custom", "")).endswith(":textarea"):
            found.append((str(fid), str(f.get("name", fid))))
    if pinned is not None:
        matches = [pair for pair in found if pair[1].lower() == pinned.lower()]
        if not matches:
            raise SystemExit(
                f"--textarea-field {pinned!r}: no multi-line text custom field by that name on "
                f"this issue type's create screen (found: {[name for _, name in found]})."
            )
        return matches[0]
    return found[0] if found else None


async def build_state(engine: Any, args: argparse.Namespace) -> tuple[StateFile, Plan, list[dict[str, Any]]]:
    """Reflect the tenant, pick the issue type, bind ids and custom fields
    into an in-memory state (the same name-matching `stint stamp` does), and
    return the plan plus the raw createmeta types for the group-1 check."""
    snapshot = await engine.dialect.reflect()
    state = StateFile(env="smoke-fixes", jira_url=args.url)

    projects = {p.key: p for p in snapshot.projects.values()}
    proj = projects.get(args.project_key)
    if proj is None:
        raise SystemExit(f"project {args.project_key!r} not found on the tenant (visible: {sorted(projects)})")
    if proj.style == "next-gen":
        raise SystemExit(
            f"project {args.project_key!r} is team-managed (next-gen); this smoke exercises the "
            f"CMP data plane — point it at a company-managed project."
        )
    state.projects[args.project_key] = ProjectMapping(id=proj.id, key=proj.key)

    # The fixed get_createmeta call itself: with the old expand=fields the
    # per-type `fields` dicts below come back empty.
    meta = await engine.dialect.get_createmeta(args.project_key)
    types: list[dict[str, Any]] = [
        {
            "name": str(t.get("name", "")),
            "subtask": bool(t.get("subtask")),
            "fields": t.get("fields") or {},
            "required": [
                (fid, str(f.get("name", fid)))
                for fid, f in (t.get("fields") or {}).items()
                if fid not in {"summary", "project", "issuetype", "reporter", "parent"} and f.get("required")
            ],
        }
        for p in (meta.get("projects") or [])
        for t in (p.get("issuetypes") or [])
    ]

    pool = [t for t in types if not t["subtask"]]
    if args.task_type:
        matches = [t for t in pool if t["name"].lower() == args.task_type.lower()]
        if not matches:
            raise SystemExit(
                f"--task-type {args.task_type!r} is not creatable on {args.project_key!r} "
                f"(creatable: {[t['name'] for t in types]})."
            )
        entry = matches[0]
    elif pool:
        entry = sorted(pool, key=lambda t: (len(t["required"]), t["name"]))[0]
    else:
        raise SystemExit(
            f"no creatable non-subtask issue type on {args.project_key!r} "
            f"(creatable: {[t['name'] for t in types]}); nothing to smoke."
        )

    # Bind the type id from the global snapshot (project-scoped ids are
    # invalid for POST /issue on CMP; see tools/smoke_dataplane.py).
    globals_by_name: dict[str, list[Any]] = {}
    for it in snapshot.issuetypes.values():
        if it.project_scoped:
            continue
        globals_by_name.setdefault(it.name.lower(), []).append(it)
    candidates = globals_by_name.get(entry["name"].lower()) or []
    if len(candidates) > 1:
        raise SystemExit(
            f"issue type name {entry['name']!r} matches multiple global issue types "
            f"({[c.id for c in candidates]}); refusing to guess. Rename the sandbox type "
            f"or pass --task-type."
        )
    if not candidates:
        raise SystemExit(
            f"issue type {entry['name']!r} is creatable on {args.project_key!r} but not in the "
            f"global /issuetype list (global types: {sorted(globals_by_name)})."
        )
    state.issuetypes[entry["name"]] = SimpleMapping(id=candidates[0].id)
    print(f"state:   {entry['name']!r} -> issuetype id {candidates[0].id}")

    # Discover a multi-line text custom field on this type's create screen,
    # or stand one up through stint's own dialect ops (--provision-textarea,
    # mirrors --provision-subtask in tools/smoke_dataplane.py).
    textarea_binding = find_textarea(entry, args.textarea_field)
    if textarea_binding is None and args.provision_textarea:
        textarea_binding = await provision_textarea(engine, proj, snapshot, candidates[0].id, args)

    # Required-on-create fields (except the textarea, which binds separately
    # below and is satisfied by the smoke's own notes value).
    provided = {k.lower(): v for k, v in (a.split("=", 1) for a in args.required_field)}
    required: list[RequiredField] = []
    consumed: set[str] = set()
    for fid, field_name in entry["required"]:
        if textarea_binding is not None and fid == textarea_binding[0]:
            continue
        key = field_name.lower()
        raw = provided.get(key)
        snap = snapshot.custom_fields.get(fid)
        if raw is None:
            hint = f" (live options: {', '.join(snap.options)})" if snap is not None and snap.options else ""
            raise SystemExit(
                f"issue type {entry['name']!r} requires {field_name!r} ({fid}) on create. Pass "
                f'--required-field "{field_name}=<value>"{hint} (repeatable).'
            )
        consumed.add(key)
        if snap is None:
            raise SystemExit(f"required field {field_name!r} ({fid}) is not in the reflect snapshot; cannot shape it.")
        required.append(RequiredField(field_id=fid, name=field_name, raw=raw, snapshot=snap))
        state.custom_fields[fid] = CustomFieldMapping(id=fid)
    leftover = sorted(set(provided) - consumed)
    if leftover:
        print(f"  warn: unused --required-field value(s): {leftover}", file=sys.stderr)

    textarea: RequiredField | None = None
    textarea_fid: str | None = None
    textarea_hint: str | None = None
    if textarea_binding is not None:
        fid, display = textarea_binding
        textarea_fid = fid
        snap = snapshot.custom_fields.get(fid)
        if snap is None:
            raise SystemExit(f"textarea field {display!r} ({fid}) is not in the reflect snapshot; cannot declare it.")
        textarea = RequiredField(field_id=TEXTAREA_ALIAS, name=display, raw="", snapshot=snap)
        # alias -> real id: the payload builder resolves through state, and
        # the changelog group proves fieldId maps back to the alias.
        state.custom_fields[TEXTAREA_ALIAS] = CustomFieldMapping(id=fid)
        print(f"state:   textarea {display!r} -> {fid} (alias {TEXTAREA_ALIAS!r})")
    else:
        # Nothing on THIS type: point at a sibling type that has one, so the
        # SKIP message names the exact rerun that closes the gap.
        for other in pool:
            if other is entry:
                continue
            found = find_textarea(other, None)
            if found is not None:
                textarea_hint = (
                    f"type {other['name']!r} has {found[1]!r} ({found[0]}) on its create "
                    f"screen — rerun with --task-type {other['name']!r} "
                    f"(adjust --required-field to that type's requirements)"
                )
                break

    model = build_model(
        name="SmokeFixesModel", alias=entry["name"], title=entry["name"], required=required, textarea=textarea
    )
    kwargs = {f"req_{i}": _parse_value(rf) for i, rf in enumerate(required)}
    return (
        state,
        Plan(
            task=entry["name"],
            model=model,
            kwargs=kwargs,
            textarea=textarea,
            textarea_fid=textarea_fid,
            textarea_hint=textarea_hint,
        ),
        types,
    )


async def run(args: argparse.Namespace) -> int:
    checks = Checks()
    if not os.environ.get("STINT_TOKEN"):
        raise SystemExit("export STINT_TOKEN (and STINT_USER for api-token auth)")
    auth = _build_auth(args.auth, "STINT_TOKEN", "STINT_USER")
    # This smoke is Cloud-only by design (fixes 2 and 4 are Cloud behaviors),
    # so the dialect is pinned: resolve_dialect_name has no domain detection,
    # a bare https:// URL alone would answer "no dialect given".
    engine = create_engine(args.url, auth=auth, dialect="jira_cloud", verify_ssl=not args.no_verify_ssl)

    created: list[IssueType] = []

    def bail(label: str, detail: str) -> int:
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
        state, plan, meta_types = await build_state(engine, args)
        print(f"tenant:  {args.url}")
        print(f"project: {args.project_key} (company-managed)")
        print(f"type:    {plan.task}")

        # ── group 1: createmeta expand (fix 3) ────────────────────────
        empty = [t["name"] for t in meta_types if not t["fields"]]
        checks.check(
            "createmeta answers per-type create-screen fields",
            bool(meta_types) and not empty,
            f"{len(meta_types)} type(s)"
            + (f"; fields missing for: {empty} (the expand=fields signature)" if empty else ""),
        )

        # engine.check on a model declaring exactly what createmeta says the
        # type can create: the user-facing path fix 3 repaired. Reflects the
        # tenant again (its own full reflect), so this is the slow step.
        print("note:    engine.check re-reflects the tenant; this can take a while")
        report = await engine.check(build_check_project(plan.model, args.project_key), state)
        for warning in report.warnings:
            print(f"    warn: {warning}")
        checks.check(
            "engine.check accepts the creatable model (no false 'not on create screen')",
            report.ok,
            "; ".join(report.errors[:3]) if report.errors else f"{len(report.warnings)} warning(s)",
        )

        try:
            async with AsyncSession(engine, state) as session:
                # ── group 2: link direction (fix 1) ───────────────────
                a = plan.model(summary="smoke fixes: link A", **plan.kwargs)
                b = plan.model(summary="smoke fixes: link B", **plan.kwargs)
                session.add(a, project=args.project_key)
                session.add(b, project=args.project_key)
                await commit(session, a, b)
                if not a.key or not b.key:
                    return bail("insert two issues for the link group", "no key assigned")
                print(f"state:   created {a.key}, {b.key}")

                posts = 0
                raw_create_link = engine.dialect.create_issue_link

                async def counting_create_link(**kw: Any) -> None:
                    nonlocal posts
                    posts += 1
                    await raw_create_link(**kw)

                engine.dialect.create_issue_link = counting_create_link  # ty: ignore
                try:
                    await session.link(a.key, b.key, link_type="Blocks")
                    links_a = await session.links(a.key)
                    ok = (
                        len(links_a) == 1
                        and links_a[0].type == "Blocks"
                        and links_a[0].direction == "outward"
                        and links_a[0].other_key == b.key
                    )
                    checks.check(
                        "link(A, B, Blocks) reads back as A blocks B (outward)",
                        ok,
                        f"links(A)={[(lnk.type, lnk.direction, lnk.other_key) for lnk in links_a]}",
                    )
                    links_b = await session.links(b.key)
                    ok = len(links_b) == 1 and links_b[0].direction == "inward" and links_b[0].other_key == a.key
                    checks.check(
                        "links(B) reads the same link as inward",
                        ok,
                        f"links(B)={[(lnk.direction, lnk.other_key) for lnk in links_b]}",
                    )
                    await session.link(a.key, b.key, link_type="Blocks")
                    links_a = await session.links(a.key)
                    checks.check(
                        "re-link is an idempotent no-op (one POST, one link)",
                        posts == 1 and len(links_a) == 1,
                        f"{posts} POST(s), {len(links_a)} link(s)",
                    )
                    await session.link(b.key, a.key, link_type="Blocks")
                    links_b = await session.links(b.key)
                    pairs = sorted((lnk.direction, lnk.other_key) for lnk in links_b)
                    checks.check(
                        "reverse pair (B blocks A) coexists with the first",
                        pairs == sorted([("inward", a.key), ("outward", a.key)]),
                        f"links(B)={pairs}",
                    )
                finally:
                    engine.dialect.create_issue_link = raw_create_link  # ty: ignore

                # ── groups 3 + 4: textarea ADF + changelog aliases ────
                if plan.textarea is None or plan.textarea_fid is None:
                    why = (
                        f"no multi-line text custom field on {plan.task!r}'s create screen — "
                        f"add one in the UI or pin an existing one with --textarea-field NAME"
                    )
                    if plan.textarea_hint:
                        why += f"; or {plan.textarea_hint}"
                    checks.skip("textarea ADF + changelog aliases (fixes 2 + 4)", why)
                else:
                    original = "smoke line one\n\nsmoke line two"
                    c = plan.model(summary="smoke fixes: textarea", notes=original, **plan.kwargs)
                    session.add(c, project=args.project_key)
                    await commit(session, c)
                    if not c.key:
                        return bail("insert with textarea value (ADF on create)", "no key assigned")
                    print(f"state:   created {c.key}")
                    reread = await session.get(plan.model, c.key, fresh=True)
                    ok = reread is not None and reread.notes == original
                    checks.check(
                        "textarea insert + hydrate round-trips as plain text",
                        ok,
                        f"notes={reread.notes!r}"[:160] if reread is not None else f"GET {c.key} returned nothing",
                    )

                    managed = await session.get(plan.model, c.key)
                    if managed is None:
                        return bail("textarea update accepted (ADF on update)", f"GET {c.key} returned nothing")
                    managed.notes = "smoke revised"
                    await session.commit()
                    after = await session.get(plan.model, c.key, fresh=True)
                    ok = after is not None and after.notes == "smoke revised"
                    checks.check(
                        "textarea update accepted (ADF on update)",
                        ok,
                        f"notes={after.notes!r}"[:160] if after is not None else f"GET {c.key} returned nothing",
                    )

                    # fix 4: Cloud items carry the display name in `field` and
                    # the id in `fieldId`; the alias must come back, not the
                    # display name, not the raw id.
                    changes = [change async for change in session.changelog(c.key)]
                    fields = [change.field for change in changes]
                    checks.check(
                        "changelog maps fieldId -> declared alias",
                        TEXTAREA_ALIAS in fields,
                        f"fields={fields[:8]} (display name was {plan.textarea.name!r}, id {plan.textarea_fid})",
                    )
                    checks.check(
                        "changelog keeps system field names unmapped",
                        "summary" in fields,
                        f"fields={fields[:8]}",
                    )
                    mine = [change for change in changes if change.field == TEXTAREA_ALIAS]
                    ok = bool(mine) and mine[-1].to_value == "smoke revised"
                    checks.check(
                        "changelog carries the readable new value",
                        ok,
                        f"to_value={mine[-1].to_value!r}" if mine else "no changelog item for the textarea",
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
    skipped = f" with {checks.skips} skipped group(s)" if checks.skips else ""
    print(f"SMOKE PASSED{skipped}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # SMOKE_* (the tools/ convention) or plain JIRA_* / JIRA_PROJECT.
    parser.add_argument(
        "--url",
        default=os.environ.get("SMOKE_JIRA_URL") or os.environ.get("JIRA_URL"),
        help="your tenant, e.g. https://you.atlassian.net (env: SMOKE_JIRA_URL or JIRA_URL)",
    )
    parser.add_argument(
        "--project-key",
        default=os.environ.get("SMOKE_PROJECT") or os.environ.get("JIRA_PROJECT"),
        help="sandbox project key (env: SMOKE_PROJECT or JIRA_PROJECT)",
    )
    parser.add_argument("--auth", choices=["api-token", "pat", "basic"], default="api-token")
    parser.add_argument(
        "--task-type", default=None, help="pin the issue type name (default: fewest-required creatable non-subtask)"
    )
    parser.add_argument(
        "--textarea-field",
        default=None,
        help="pin the multi-line text custom field by display name (default: first discovered)",
    )
    parser.add_argument(
        "--provision-textarea",
        action="store_true",
        help="no textarea field on the create screen? create (or reuse by name) a paragraph "
        "custom field and add it to the project's create/edit screens and field configuration "
        "through stint's dialect ops. Persists after the run.",
    )
    parser.add_argument(
        "--textarea-name",
        default="Smoke Notes",
        help="display name for --provision-textarea (default: Smoke Notes; reuses a same-named field)",
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
        missing = [name for name, value in (("--url", args.url), ("--project-key", args.project_key)) if not value]
        raise SystemExit(
            f"missing {' and '.join(missing)}. Pass them or export the env vars this script "
            f"reads: SMOKE_JIRA_URL (or JIRA_URL), SMOKE_PROJECT (or JIRA_PROJECT), plus "
            f"STINT_TOKEN and STINT_USER for auth. Example:\n"
            f"  STINT_USER='you@example.com' STINT_TOKEN='...' \\\n"
            f"  uv run python tools/smoke_dataplane_fixes.py \\\n"
            f"      --url https://you.atlassian.net --project-key PLAT"
        )
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
