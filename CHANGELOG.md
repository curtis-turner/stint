# Changelog

All notable changes to stint are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-09-27

### Added
- `stint revision --autogenerate` now scopes the desired-snapshot diff to
  the `Project` classes defined in the module passed via `--schema`
  (matched on `__module__`), plus everything reachable from them:
  issuetypes, custom fields, screens, screen schemes, and field
  configurations. Importing a sibling schema module for reuse (the
  documented sharing pattern) no longer leaks its `Project`s into the
  generated migration. The migration file's preamble lists the project
  keys it covers, so a schema author notices at a glance if scope
  misbehaves. Unscoped invocations keep the historical behavior
  (`build_desired_snapshot()` without `project_module`). (Closes the
  CMP-B portion of `CMP_ISSUE_DRAFT.md`.)
- `stint apply` (team-managed projects) now persists the state file
  after every successful op, matching CMP's per-op `ctx.persist()`
  pattern. If a mid-plan op fails, every change that already wrote to
  Jira is on disk; a retry sees those ids in `tmp_state` and does not
  duplicate them, and a later `--allow-delete` run can target anything
  left orphaned from a previous attempt. (Closes the fix-forward
  half of the TMP-C finding in `ISSUE_DRAFT.md`.)
- Translates Jira Cloud's retired-classic-field-configurations 400
  (`Cannot create a new field configuration. Please use Field Scheme
  instead.`) into a `ConfigurationError` that points at RFC-103/104/105
  and the in-tree Phase 3 plan for the v1/v2 Field Schemes split.
  Schema authors get an actionable message instead of a raw HTTP
  error. The real fix (a per-tenant v1/v2 capability detection routing
  to Field Schemes) is the larger Phase 3 work. (Closes the short-term
  half of the CMP-A finding in `CMP_ISSUE_DRAFT.md`.)
- `stint.dialects.jira.tmp.types` (`tmp_type_key_for`,
  `TMP_FIELD_TYPE_KEYS`) names the 9-of-18 subset of Jira custom-field
  type ids that the TMP `createCustomFieldInProjectAndAddToAllIssueTypes`
  mutation actually accepts. `build_tmp_desired` raises
  `TmpFieldTypeError` (a `ConfigurationError`) eagerly during
  `stint apply` plan time when a schema declares an unsupported type,
  surfacing the gap with a precise message naming the offending field
  alias and the supported set instead of a raw `400 Invalid field type
  specified` from the live mutation. `tools/tmp_type_probe.py` is the
  live discovery tool to re-verify the count against a real tenant.
  (Closes TMP-B in `ISSUE_DRAFT.md`.)
- `tmp_set_layout` now adds layout items for newly-declared custom
  fields, not just prunes them. The previous behavior wrote the empty
  `current.items` of a freshly-created work type back unchanged, so
  declaring a new `IssueType` together with its fields in the same
  schema produced a work type whose edit screen showed none of the
  declared fields -- and `apply` reported full success. New items come
  from (1) another work type's layout in the snapshot, (2) a fresh
  `read_layout` of a sibling work type the apply run knows about, or
  (3) minimal synthesis from the desired-field metadata, in that order.
  (Closes TMP-A in `ISSUE_DRAFT.md`.)
- `stint.dialects.jira.common.paginate` now raises `ReflectionError`
  on (a) a dict response with no `values` key (an unrecognized
  pagination envelope), and (b) a paginated response that echoes a
  `startAt` different from the one we requested. Both used to be
  silent misbehavior: the first would leave live resources invisible
  to diff; the second would loop on the same page. (Closes DC-A in
  `DC_ISSUE_DRAFT.md`.)
- Four respx-driven offline repros in `tools/` exercising the
  Phase 0/1/2 fixes against a mocked Jira (no credentials required):
  `tools/tmp_apply_eager_error_repro.py`, `tools/tmp_apply_layout_repro.py`,
  `tools/cmp_autogenerate_scoping_repro.py`, `tools/cmp_retired_fc_repro.py`.
- `tools/tmp_type_probe.py`: live vocabulary probe for the TMP custom-
  field type mapping. Iterates every supported type against a real
  team-managed project, deletes each one it created, and exits non-zero
  with a clear drift message if the live tenant disagrees with the
  in-tree map.

### Changed
- Collapses the Jira dialect layer to a single `JiraCloudDialect`
  class in `stint/dialects/jira/cloud.py` and deletes the legacy
  `JiraDialectBase` (`_base.py`). DC-specific ClassVar defaults
  (`/rest/api/2`, `expected_deployment_type="Server"`, etc.) are gone;
  Cloud's overridden behavior is now the only behavior. The DC vs
  Cloud split in `paginate()`, `add_custom_field_option`,
  `_reflect_field_options`, project CRUD, and search has been folded
  into the single class. The README + `common.py` docstrings stop
  mentioning DC. (Closes DC-B in `DC_ISSUE_DRAFT.md`.)

## [0.3.0] - 2026-07-05

### Added
- Experimental, opt-in support for Jira Cloud team-managed ("next-gen")
  projects via a new `jira_cloud_tmp` dialect (`stint.dialects.jira.tmp`).
  The public Jira Cloud REST API cannot author team-managed project config
  (fields, work types, layouts); this dialect drives Atlassian's
  undocumented internal APIs instead, isolated from the company-managed
  path so a CMP-only user's process never loads it. Covers project-scoped
  custom fields (create/edit/delete, including select options), work types
  (create/delete), and issue layouts (read/write field association), one
  project at a time. (Closes #13.)
  - `stint.engine.create_tmp_engine`/`TmpEngine`: a separate engine from
    `Engine`/`create_engine`, selected via a `jira_cloud_tmp+https://...`
    URL prefix or `dialect="jira_cloud_tmp"`.
  - `stint reflect --dialect jira_cloud_tmp --project-key <KEY>` reflects
    one team-managed project into a snapshot.
  - New `stint apply` command: reflects, diffs against schema, and writes
    in one run. Team-managed writes are full-replacement with no
    migration-file or downgrade equivalent, so `apply` reconciles live
    state directly against the schema on every run — terraform-style:
    prints a plan, then requires typing `yes` to write (or
    `--auto-approve`/`--dry-run`).
  - `StateFile.tmp_projects` persists each team-managed project's
    field/work-type/layout id mappings.
  - Emits a `UserWarning` on first use noting the dialect is experimental
    and unsupported.
  - Two example walkthroughs, split by dialect: `examples/company_managed/`
    (existing, moved) and `examples/team_managed/` (new), each with its
    own README.

### Changed
- README install instructions now lead with `uv add stint`; `pip install
  stint` is kept as a documented alternative.

## [0.2.0] - 2026-07-02

### Changed
- Lowered the minimum Python version from 3.14 to **3.10**, so users on
  distro-shipped interpreters (RHEL, Ubuntu LTS) can install stint. PEP 695
  generics were rewritten as `TypeVar`/`Generic` and `datetime.UTC` as
  `timezone.utc`; behaviour is identical across 3.10–3.14. The one exception:
  the shadowed-CustomField diagnostic needs PEP 649 (3.14+); on older versions
  the same mistake surfaces as a `NameError` instead of stint's tailored
  message. CI now runs the full matrix, and `scripts/test-all.sh` reproduces it
  locally via uv.
- Corrected dependency floors that were too low to actually work: `pydantic`
  now requires `>=2.7` (older versions cannot resolve the schema models'
  deferred annotations) and `cyclopts` requires `>=4.0` (the CLI uses
  `result_action`, added in 4.0). A new CI `floors` job installs the declared
  minimums on Python 3.10 and runs the suite, so the `>=` bounds stay honest.

## [0.1.0] - 2026-06-30

### Added
- `examples/README.md`: a runnable end-to-end walkthrough (validate → stamp →
  autogenerate → upgrade) against a real Jira Cloud tenant, plus a committed
  env-config template `examples/devel.env.example.yaml` to copy into `.stint/`.

### Fixed
- Issue-type matching now considers **only global** issue types. A tenant with
  team-managed projects exposes same-named project-scoped types in
  `/issuetype`; `stamp` and `create_issuetype` could record one of those, and a
  later global update failed with `not a global issue type`. The reflected
  snapshot now carries `project_scoped` (from Jira's `scope`), and matching
  ignores project-scoped types. (Full CMP/TMP style-aware scoping tracked in
  #11.)
- `stamp` now adopts **issue type schemes** (it previously skipped them), so a
  clean stamp no longer leaves autogenerate re-emitting `create_issuetype_scheme`.
- `revision --autogenerate` no longer emits an `update_project` lead change on
  every run. The schema declares `__lead__` as an email but the snapshot
  reports an accountId, so the two were never comparable. Lead drift on an
  existing project is not auto-detected; set it on create or via a hand-written
  migration. (#7 follow-up)
- The CLI prints domain errors (transport, auth, config) as a single `ERROR:`
  line on stderr with exit 1, instead of dumping a Python traceback.
- Reflection collapses skipped team-managed synthetic screens into **one**
  consolidated warning instead of one per screen (a busy tenant produced dozens).
- A dotted `--schema` path (e.g. `schemas.platform`) now resolves from the
  working directory under the installed `stint` console script, matching the
  documented quickstart.
- `create_issuetype` now adopts an existing same-named issue type instead of
  POSTing a duplicate. Every Jira tenant ships built-in types (Bug, Task,
  Story, Epic, Subtask) and enforces globally-unique names, so a greenfield
  `upgrade` against a real tenant used to 409 on the first `create_issuetype`.
  The op reflects issue types, and on a name match records the existing id in
  state and skips the create. (#8)
- `revision --autogenerate` refuses to run when migrations are written but not
  yet applied, instead of stacking a duplicate migration that recreates the
  same objects. Apply the pending migrations with `stint upgrade` first, or
  pass `--force` to stack anyway. (#6)

## [0.1.0a2] - 2026-06-26

### Changed
- **Require Python 3.14+** (`requires-python = ">=3.14"`). stint depends on
  PEP 649 deferred annotation evaluation — the default from 3.14 — so the
  schema metaclass can inspect `Annotated` field metadata reliably (e.g.
  detecting a CustomField that shadows its attribute name). On 3.10–3.13 that
  same code path raised a raw `NameError` at class-definition time. CI now
  tests 3.14 only.
- `__lead__` now takes a project-lead **email** that stint resolves to the
  backend user id at apply time (DC username, Cloud `accountId`) via the
  user-search API. This fixes project create/update on Cloud, which rejects
  a username as `leadAccountId`. A raw username/accountId (no `@`) is passed
  through unchanged. Resolution requires the "Browse users and groups"
  permission; a 403 surfaces as a `ConfigurationError` with guidance. (#7)

## [0.1.0a1] - 2026-06-26

### Added
- Sync `Session` facade over `AsyncSession` for callers who do not want to
  manage an event loop.
- `stint validate` CLI subcommand for schema-level checks with no network
  calls.
- `stint/py.typed` PEP 561 marker, shipped via
  `[tool.setuptools.package-data]`. Type checkers now honor the inline
  annotations against the installed package.

### Changed
- CLI ported from `argparse` to [Cyclopts](https://cyclopts.readthedocs.io)
  for type-hint-driven parsing and Rich-rendered help. Subcommand names,
  flag names, and exit codes are unchanged. `--merge a b c` still accepts
  space-separated revisions.
- Repositioned 0.1 targets: **Jira Cloud (CMP + TMP) is primary**; **Jira
  DC is fast-follow**. The dialect code is unchanged and both still ship,
  but live-tenant validation will land on Cloud before DC. README and
  plan reflect the new ordering.

### Removed
- `[project.optional-dependencies].dev` block from `pyproject.toml`. It
  duplicated `[dependency-groups].dev` with stale lower bounds and leaked
  test/lint tooling into `pip install stint[dev]`. `uv sync --dev` only
  read the dependency group anyway.

### Fixed
- Cloud reflect now reads custom fields from the paginated
  `GET /rest/api/3/field/search`, not `GET /rest/api/3/field`. The latter
  returns only a subset of custom fields on Cloud (omitting fields not yet
  on a screen, including freshly created ones), so reflect missed fields
  stint had just created, which broke create-then-reflect round-trips
  (`autogenerate`/`stamp` reporting created fields as missing). (#9)

## [0.1.0a0]

Initial alpha. The schema plane and the data plane are both shippable
end-to-end against Jira Data Center and Jira Cloud.

### Added
- Declarative schema classes: `IssueType`, `Project`, `CustomField`, `Screen`,
  `ScreenScheme`, `FieldConfiguration`.
- Jira DC and Jira Cloud dialects sharing a common base.
- Reflection of all in-scope admin objects into a `Snapshot`.
- Migration package: `Migration`, `RevisionGraph`, op API (30 functions),
  runner with mid-op state persistence, `op.unsupported` escape hatch,
  multi-parent merges.
- `stint revision --autogenerate` for diffing schema against a live env.
- `stint stamp` for brownfield adoption.
- HTTP retry with `Retry-After` honoring, advisory lock on state file,
  env config loader.
- Async data plane: `AsyncSession` with identity map, dirty tracking,
  `select(...).where(...)` compiling to JQL, `session.add/delete/commit`.
- TMP awareness: project style tracked in state, CMP-only ops raise
  `UnsupportedTMPOpError` with a deep link to the Jira UI.

[Unreleased]: https://github.com/curtis-turner/stint/compare/v0.1.0a0...HEAD
[0.1.0a0]: https://github.com/curtis-turner/stint/releases/tag/v0.1.0a0
