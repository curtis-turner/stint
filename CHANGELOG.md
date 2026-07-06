# Changelog

All notable changes to stint are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Ten more standard Jira custom field types in `stint.fields`, closing the
  gap between stint's declarative model and Jira's built-in field type
  catalog: `RadioButtonsField`, `CheckboxesField`, `LabelsField`,
  `URLField`, `VersionField`, `MultiVersionField`, `GroupField`,
  `MultiGroupField`, `MultiUserField`, `ReadOnlyField`. Each is wired
  through schema validation (`CustomField` options rules,
  Literal-vs-options checks for the option-style types), write payloads,
  read hydration, and autogenerate's reflect-back type lookup. (Closes #15.)

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
