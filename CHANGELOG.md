# Changelog

All notable changes to stint are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
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
