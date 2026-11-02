# Changelog

All notable changes to stint are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.1] - 2026-11-01

### Fixed

- `stint reflect` (and YAML/JSON printing of any snapshot) no longer crashes
  with `TypeError: cannot pickle 'mappingproxy' object`: snapshots freeze
  mapping fields as `MappingProxyType`, which `dataclasses.asdict` cannot
  deepcopy; the serializer now decodes dataclasses field-by-field.
- `stint revision --autogenerate` no longer re-emits
  `update_custom_field(searcher_key=...)` on every run. `GET /field/search`
  only returns `searcherKey` when the request lists it under `expand`, so
  reflection saw every field with an empty searcher and diffed each declared
  searcher as phantom drift — even though Jira was in sync. The reflect
  request now sends `expand=searcherKey`, and my earlier PUTs had actually
  applied correctly all along.

- `stint revision --autogenerate` no longer re-emits
  `add_issue_security_level_member` for user members declared as emails on
  every run (and `stint upgrade` no longer then dies with Jira's
  `"Issue security scheme level member with the type user already exists."`
  400). The diff compared the declared email against recorded/reflect
  *accountIds*, which never match. `revision` now resolves member emails
  through the user-search API while the engine is open and passes the map to
  `diff(member_params=...)`; the op resolves before its idempotency check so
  an email-declared re-run stops before the wire; and the dialect treats
  Jira's exact duplicate-member 400 as the member being present instead of
  aborting upgrades that mixed fresh and existing members.
- Reflection of security-level members now paginates the
  `/issuesecurityschemes/level/member` endpoint (a single page silently
  truncated and generated phantom member adds past the cut), falls back to
  that endpoint when a scheme GET carries inline `"members": []`, and keys
  member entries without an `id` synthetically instead of collapsing them
  into one entry.

## [0.7.0] - 2026-11-01

### Added

- Issue security schemes are now schema-plane objects end to end:
  `SecurityLevel` (with `members=[SecurityLevelMember(...)]`), `SecurityMember`,
  and `SecurityScheme` declarations register with the schema, reflect with
  levels *and* holder-parametrized members (the list endpoint omits levels,
  so reflect follows up with the per-scheme GET and the level-member
  endpoint), and diff into `CreateIssueSecurityScheme` / `AddSecurityLevel` /
  `AddSecurityLevelMember` / `SetDefaultSecurityLevel` /
  `ClearDefaultSecurityLevel` / `SetProjectIssueSecurityScheme` ops. The
  state file tracks each scheme's `level_ids`, `default_level_id` and the
  *resolved* `member_values` (accountIds, not emails) so applied members stay
  idempotent across email→accountId resolution.
- `default_level=None` clears a scheme's live default
  (`op.clear_default_issue_security_level`, `PUT
  /issuesecurityschemes/level/default` with `defaultLevelId: null`). Before
  this the diff bailed out on a `None` desired default, so a default set
  earlier could never be unset.
- `AsyncSession.get(Model, key, fresh=True)` bypasses the identity map and
  refreshes the cached instance in place. Default `get()` still serves the
  cache, including instances just inserted.
- Runnable end-to-end ORM example:
  `examples/company_managed/create_and_query_issues.py` (insert → fresh
  read-back → JQL query → dirty update → delete, with `--keep`). The old
  `platform.py` example is renamed `platform_schema.py` so the module no
  longer shadows the stdlib `platform` for CLI imports that `sys.path`-load
  the file; `security_scheme.py` demonstrates the security-scheme feature
  alongside the data plane.

### Fixed

- Custom fields created via `op.create_custom_field` now carry Jira's
  `searcherKey`: every `_FieldType` declares the searcher its type needs
  (select → multiselectsearcher, text → textsearcher, …). Omitting it
  creates a field with search template None — writes and reads work, but
  every JQL clause on the field fails with "Your query couldn't be
  processed" (live tenant, 2026-11). Reflection carries `searcherKey` into
  `CustomFieldSnapshot`, and autogenerate repairs existing fields missing
  it with `op.update_custom_field(..., searcher_key=...)`.
- `POST /field`'s response carries no description, so the create op applies
  it with a follow-up `PUT` — previously the snapshot drifted from the
  declaration on the first apply (and select-style reflects rebuilt the
  snapshot without it).
- `reporter` is stripped from insert payloads (Jira fills it from the
  caller's identity and 400s on an explicit value) while staying
  settable on updates (`PUT`) and hydrated (as an accountId) on reads.
- Company-managed `create_project` now defaults Cloud CMP creates to the
  `gh-kanban-template` (Jira silently fell back to a *team-managed*
  template otherwise, and that project ignores its assigned schemes), and
  rejects a template whose style contradicts the declared `style` before
  the project is created in the wrong shape.
- `PUT /field`'s `parameters` errors list per-item "Field 'summary'
  cannot have required changed" as a tolerated no-op instead of failing
  the whole field-scheme item set (live tenant, 2026-11).

### Fixed

- `paginate()` no longer spin-detects only `startAt`-based pagination (L1):
  cursor-paginated responses carrying a `nextPage` URL are followed
  verbatim (with a self-referential-cursor guard), and a server that clamps
  an out-of-range `startAt` echo now terminates iteration instead of
  raising mid-reflect (L4). The docstring matches the implementation.
- `_default_context_id` prefers the context marked `isGlobalContext: true`
  (L2) — the API does not order the default context first, so option
  add/delete could target a scoped context.
- `update_issue_security_scheme` with no changes no longer PUTs an empty
  body (L3) — Jira rejects `{}` with a 400, turning a no-op apply into a
  spurious failure.
- `PartialCommitError` no longer masks its construction with an IndexError
  when there are no failures (L5).
- The default HTTP `User-Agent` tracks the installed version
  (`stint/<__version__>`) instead of the hardcoded `stint/0.1` (L8),
  overridable via the `user_agent` argument.
- HTTP error messages name the endpoint but no longer echo the query
  string (L7) — e.g. the user email passed to account search as `?query=`.
- A dialect/engine construction failure now closes the freshly opened
  httpx pool synchronously instead of leaking it until GC (L9).
- `Session.limit(0)` makes no HTTP call and returns `[]`; `limit(n)` page
  size is clamped to Jira's documented 100 (L10).
- JQL string literals escape `\n`/`\r`/`\t` (L11): multi-line values no
  longer produce an opaque 400.
- Snapshot dict fields (`CustomFieldSnapshot.options`,
  `ScreenSchemeSnapshot.mappings`, `FieldConfigurationSnapshot.items`) are
  read-only views so the `frozen=True` promise is real (L17); dict-typed
  state file data is untouched.
- Schema module loading: same-stem files in different directories no
  longer share one synthetic module slot (L18), and re-executing a schema
  module replaces its own registrations instead of raising `Duplicate ...
  alias` — same-alias declarations from distinct modules still raise.
- `stint revision --autogenerate` re-checks the migration graph's head
  after the (slow) network reflect (L19): two racing runs can no longer
  both parent to the same head and leave a branched graph.
- `stint stamp` now skips ambiguous display names with a `skipped`
  report naming every candidate id (L20) instead of silently binding an
  arbitrary Jira duplicate.
- Missed-drift gaps now surface as warnings instead of "no changes
  detected" (L21): declared screen fields absent from the reflected
  screen, declared FC items (with required/hidden flags) drifting from the
  reflected configuration; custom-field and project descriptions are now
  reflected and diffed (existing project description changes emit
  `op.update_project(description=...)`).
- `stint history` renders merge migrations' tuple parents as truncated IDs
  (L16) instead of printing a sliced tuple.
- `upgrade --to <current revision>` is an alembic-style no-op and
  `downgrade --to <current>` likewise; an unreachable `--to` target raises
  `MigrationGraphError` instead of a bare `ValueError` (L15).
- The env-config `verify_ssl` key must be a real YAML boolean; a quoted
  `"no"`/`"false"` string used to silently disable nothing (L23). The
  dead argparse-era `apply_env_defaults` helper is removed.
- TMP: `_find_template_remote` re-raises unexpected errors (dead token,
  5xx) instead of degrading the run to a synthesized template item (L25);
  a rename earlier in the same apply run is patched into the subsequent
  `SetLayout` write, so the server no longer echoes back the stale field
  name (L26); `resolve_project` raises on a missing uuid instead of
  failing later with an opaque 400 from `create_worktype` (L28); and
  `stint/dialects/base.py` no longer claims `BaseDialect` covers
  team-managed projects (L29).
- The public `stint` surface exports the `SecurityLevel`,
  `SecurityLevelMember`, `SecurityScheme` declaration aliases and their
  snapshot types via `__all__` (L14).

### Known behavior (documented, not a bug)

- TMP `SetLayout` is a full-replacement PUT with no version/etag
  precondition (L30): UI edits to a work type's layout between an apply's
  reflect and its write (a window that includes the interactive
  confirmation prompt) will be overwritten. This is inherent to the
  declarative model.

### Fixed

- `StateLock` release is ownership-verified (H3 residue): `release()`
  confirms the on-disk `<state>.lock` is still the file this process
  created (device/inode match of the retained `O_EXCL` fd) before
  unlinking. The stale-lock workflow — an operator deletes a stale lock
  and a new holder acquires — can no longer be broken by the old holder's
  late release deleting the new holder's lock. A failed write in
  `acquire()` also removes the fresh empty lock file instead of stranding
  a permanent lockout, and the lock body carries a `token=` ownership
  line for diagnostics.

- `DeleteProject` no longer shares an apply phase with
  `DeleteIssueTypeScheme` (M4): the alphabetical tie-break could order a
  generated migration to delete an issuetype scheme before the project
  still bound to it, which Jira rejects. Projects now delete first
  (phase 79, before all scheme deletes) in both hand-run and generated
  migrations.
- `-m` messages containing triple quotes or a trailing backslash no
  longer produce unloadable migration files (M2): all three generated
  renderers sanitize the message before embedding it in the file's
  docstring (backslash then 3+-quote runs escaped, value round-trips).
  One malformed docstring previously broke loader-side
  upgrade/downgrade/history/revision for the whole migrations directory.
- An unknown custom-field type no longer emits runtime-broken code (M3):
  the generated migration keeps the `op.create_custom_field` as a comment
  and raises a `StintError` naming alias and type id at apply, instead of
  an opaque `AttributeError` on a string. The supported-type map is
  discovered from `stint.fields`, so a newly added field type can no
  longer silently drop out of it.
- Scoped (`--schema`) autogenerate no longer drops declared-but-unreferenced
  Screens and CustomFields (M5): a loose declaration registered by the
  schema module stays in the desired snapshot instead of being scoped out
  and reported as a permanent orphan (and deleted under `--allow-delete`).
  Sibling-module declarations still cannot leak — the registry now records
  each declaration's defining module and scoping matches it.
- A cyclic revision graph now fails loudly instead of looking in-sync (M6):
  `upgrade`/`history`/`current` raise `MigrationGraphError` when the graph
  has no heads (only a revision cycle produces that), instead of silently
  walking nothing. An empty migrations directory remains a no-op.
- The state file's `jira_url` no longer records a dialect prefix (M9):
  `upgrade`/`downgrade`/`stamp`/`revision` now record the base URL the way
  `apply` always has, so state files agree across commands and deep links
  built from `jira_url` stay valid.
- State files are validated against the resolved connection (M7):
  `upgrade`/`downgrade`/`apply`/`stamp`/`revision` refuse to run when the
  loaded state file records a different `env` or `jira_url` than the
  connection resolves to — previously a prod state file pointed at a dev
  env (or a typo'd `--url`) migrated the wrong tenant using the other
  tenant's id mappings. URL comparison tolerates dialect prefixes and
  trailing slashes so pre-existing files keep working.
- Corrupt or incomplete state files raise `StateFileCorruptError` (M8):
  missing top-level `env`/`jira_url` keys or a mapping entry missing its
  `id` names the exact path (e.g. `mappings.custom_fields['sev']`)
  instead of escaping as a bare `KeyError` traceback; non-mapping values
  are caught the same way.

## [0.6.1] - 2026-10-31

### Fixed

- The HTTP client now maps transport-level failures (`httpx.RequestError`:
  connect errors, DNS failures, read timeouts) to `TransportError` with
  `status_code=None` after applying the existing backoff — library users
  can `except StintError` for every network failure, dialect handlers
  catching `TransportError` finally see network errors, and transient
  timeouts are retried like 429/503 (H7).
- Retry policy tightened (M12): 503 is retried only for idempotent methods
  (GET/PUT/DELETE). A 503 can arrive after Jira committed a POST write, so
  retrying `POST /project`, `/field`, or `/issue` could create duplicates
  the state file never learns about. 429 stays safe for all methods.
- The Field Schemes capability probe only caches positive determinations
  (H6): `v2` on a successful probe, `v1` only when the server itself
  answers 404/400. Timeouts, auth errors, and other transient failures are
  served as v1 for that call but never cached, so one network blip at
  startup can no longer pin a genuine Field Schemes tenant to retired v1
  endpoints for the lifetime of the engine.
- Issue security scheme reflection now actually reflects security levels
  and members (H8): the list endpoint never carries `levels`, so each
  scheme is fetched with the single-scheme GET (members inline, or via the
  level-member endpoint otherwise). Reflect no longer reports
  `levels=[]` for every scheme, which made declared levels diff as
  permanent phantom drift.
- Bulk `projectId` filters are chunked (batch 50) across reflection and
  the Field Schemes project endpoints (M10). Jira caps these filters
  around 100 ids; larger tenants failed reflection outright (400) or
  silently truncated bindings, fabricating scheme drift.
- The v2 bulk field-removal DELETE inspects the 200 response body (M14):
  only the tolerated "not associated" per-field failure is silent; any
  other per-field rejection is surfaced as a warning naming the field ids,
  instead of discarding the body and recording a wrong desired state.
- `new delete_json()` (M14) lets callers read DELETE response bodies.
- Non-JSON 2xx bodies (captive portals, proxies, wrong base URLs) raise
  `TransportError` instead of a raw `json.JSONDecodeError`.
- Every classic (v1) field-configuration write — delete, update, set-item,
  scheme-mapping, scheme-create, project-binding — now carries the same
  retired-API detection + re-probe/retry the create path had (M15). A
  tenant migrated between probe and write no longer fails mid-apply with
  a bare `TransportError 400`.
- The work-type id cache is invalidated on issuetype create/delete (M11),
  so "visible everywhere except X" field restrictions no longer silently
  exclude freshly created work types.
- `create_issue_security_scheme` validates the create response id with the
  shared helper (M16); an empty 2xx body raises a clear error instead of
  an untracked scheme plus `AttributeError`. `_server_info` and the
  security-level/member parsers also reject unexpected payload shapes
  with `ReflectionError` instead of raw `AttributeError`/`TypeError`/`KeyError`.
- TMP: `read_layout` selects `pageInfo { hasNextPage }` on both item
  connections and raises on truncation (H4) — the layout PUT is a
  full-replacement write built from this read, so truncated reads
  silently removed untouched fields from work-type layouts. A missing
  `pageInfo` is rejected too, rather than re-opening the silent hole.
- TMP: the GraphQL client validates that a 200 body carries a dict
  `data` object (M20). Envelope drift, `{"data": null}`, or non-dict
  bodies now raise `TmpApiError` at the client boundary — including
  inside `check_capabilities`' friendly broken-tenant report — instead of
  escaping as `TypeError`/`KeyError` tracebacks. `enumerate_fields` also
  guards the unknown-project-key (`jiraProjectByKey: null`) response.
- TMP: a tracked field whose id was deleted by hand in Jira is a
  recoverable dead end (M17): the plan re-binds a same-name field via the
  adoption scan, else creates fresh, instead of emitting an update that
  aborts identically on every run. `--allow-delete` on a dead mapping now
  drops only the bookkeeping (`DropFieldMapping`) instead of DELETEing an
  id Jira no longer knows.
- TMP: field drift now covers `description` (the enumeration read selects
  it and `CustomFieldSnapshot` carries it) and `type_id`, which raises a
  precise plan-time error since a field's type cannot change in place
  (M18). Description-only changes no longer report "No schema changes"
  forever.
- TMP: work-type creation is recoverable (M19): `externalUuid` is derived
  deterministically from `(project_uuid, alias)` so a re-run re-sends the
  same id for server-side dedup, and `tmp_upsert_worktype` adopts an
  existing same-name work type instead of duplicating it.

## [0.6.0] - 2026-10-31

### Added

- Jira Cloud's Field Schemes API (v2, `/rest/api/3/config/fieldschemes`)
  is now implemented alongside the classic field-configuration API, with
  per-tenant capability detection cached on the dialect instance. On a
  tenant that speaks Field Schemes, `create_field_configuration`,
  `set_field_configuration_item`, `create_field_configuration_scheme`,
  the scheme-mapping/binding calls, reflection, and project binding all
  route to the new endpoints automatically — no schema or migration
  changes. The classic API stays available for tenants still on the old
  rollout stage, and a classic create that hits the retired-API 400
  re-probes and retries on v2 before surfacing an error. This is the real
  fix for the CMP-A finding in `CMP_ISSUE_DRAFT.md`.
- New dialect surface for the Field Schemes API:
  `create_field_scheme`/`update_field_scheme`/`delete_field_scheme`,
  `get_field_scheme_fields`, `associate_fields_with_field_scheme`,
  `remove_fields_from_field_scheme`, `set_field_scheme_field_parameters`,
  `associate_projects_with_field_scheme`,
  `get_projects_with_field_schemes`, and
  `search_field_scheme_projects`. New field schemes associate every
  system/custom field up front, matching a classic configuration's
  "visible unless explicitly hidden" default. A synthesized
  field-configuration scheme is translated into one project field scheme,
  merging its member configurations' fields; a field omitted from an
  overriding configuration is restricted to the work types that still
  include it, and per-work-type required behavior becomes inline
  `workTypeParameters` overrides. Bulk field requests are chunked under
  the API's 100-field limit.
- `op.update_field_configuration` (and the matching diff/emit support)
  closes the previous "no update op, skip name/description drift" gap.
- `tests/test_field_schemes.py`: offline respx coverage for capability
  detection, the retired-400 retry, v2 reads/writes, project binding,
  and the member-configuration merge.
- `stint apply` (team-managed projects) now adopts an existing
  project-scoped field when a declared `CustomField` has no state
  mapping but a reflected field matches it by name *and* type. Adoption
  replaces a duplicate create and records the alias→id mapping in state,
  so a field orphaned by a pre-0.5.0 partial apply (or created by hand)
  becomes reachable by the normal `--allow-delete` path. More than one
  matching untracked field raises `TmpAmbiguousFieldError` instead of
  guessing.
- `stint apply` now reports project-scoped fields that exist in Jira but
  are in neither state nor schema. Previously these were completely
  invisible: `plan_tmp` only diffs state against desired, so
  `--allow-delete` could never target them. The warning names each field
  and explains the adopt-then-delete recovery path. (Closes the
  delete-side gap called out in `ISSUE_DRAFT.md` TMP-C, beyond the
  per-op persistence shipped in 0.5.0.)
- `tools/tmp_orphan_field_recovery_repro.py`: offline repro (no
  credentials) showing an orphan adopted without duplication, a
  leftover reported, and the adopted field reaching the normal
  `DeleteField` path.

### Fixed

- Auth secrets no longer leak through dataclass `repr()`. `PATAuth.token`,
  `BasicAuth.password`, and `APITokenAuth.token` are now declared with
  `field(repr=False)`, so logging or echoing a credential object shows
  `APITokenAuth(email='you@example.com')` instead of the raw token. The
  fields stay required, participate in equality/hash as before, and remain
  visible to `dataclasses.asdict()` for serialization paths that
  consciously want the value. `tests/test_auth.py` covers masking,
  equality semantics, and `asdict`.
- State file writes are now atomic and create parent directories.
  `StateFile.save()` previously used `Path.write_text` — truncating in
  place — so a crash, Ctrl-C, or full disk mid-write could corrupt the file
  that is the only record of alias→Jira-ID mappings, and
  `stint upgrade --state state/dev.yaml` mutated Jira first and then failed
  on a raw `FileNotFoundError` when `state/` did not exist yet. Writes now
  go through a fsync'd sibling temp file moved onto the target with
  `os.replace` (atomic on POSIX and Windows, no temp file abandoned next to
  the state file on failure), parent directories are created eagerly, and
  errors surface as `StateFileWriteError` (a `StintError`), not raw
  tracebacks. `stint upgrade`/`downgrade`, `stint stamp`, and `stint apply`
  additionally validate the state path is writable *before* issuing any
  Jira writes, and state/migration-file I/O is pinned to UTF-8 regardless
  of locale (cp1252 no longer corrupts non-ASCII schema names on Windows).
- IssueTypeScheme membership drift is now applied instead of silently
  dropped. Autogenerate detected the drift (reordering a Project's
  `__issuetypes__`, or removing a member already in Jira) and emitted
  `op.update_issuetype_scheme(alias=...)`, but the op had no membership
  handling and the Jira scheme-update endpoint only accepts
  name/description/default — the migration made zero HTTP calls,
  reported success, and every subsequent autogenerate re-detected the
  same drift forever. The emitter now renders the declared member list,
  `op.update_issuetype_scheme` accepts `issuetypes=[...]` (validated to
  be non-empty and to contain the default), and the Cloud dialect
  reconciles the scheme's members via Jira's add endpoint
  (`PUT /issuetypescheme/{id}/issuetype`), per-member removal
  (`DELETE /issuetypescheme/{id}/issuetype/{issueTypeId}`), and the
  reorder endpoint (`PUT /issuetypescheme/{id}/issuetype/move`), so
  applying the migration actually converges. Jira still refuses
  removals it cannot perform (member in use by issues, last standard
  member) — that 400 now surfaces at apply time instead of being
  silently swallowed.
- State file locking now covers state loading and all writer commands
  execute lock-safe. `stint upgrade`, `stint downgrade`, and `stint
  apply` previously read the state file *before* acquiring the
  `<state>.lock` file, so a process that loaded state, waited for the
  lock, and then held it would carry a stale in-memory copy and clobber
  — on its next whole-file save — whatever a concurrently finished
  process had written, for every project in the file. State is now
  loaded inside the locked region. `stint stamp` populated and saved
  the same state file without ever taking the lock; the lock now spans
  its load-reflect-stamp-save. Finally, `stint apply` constructed its
  TMP engine between lock acquisition and the cleanup handler, so a
  construction failure leaked the lock file permanently (no
  stale-lock detection); construction now happens before the lock is
  acquired.

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
instead.`) into a `ConfigurationError` that points at the Field Schemes
  API and RFC-103/104/105. The create path first re-probes the tenant and
  retries on the v2 API; this error is the last resort when the v2 probe
  also fails. (Pairs with the full v2 routing above for CMP-A in
  `CMP_ISSUE_DRAFT.md`.)
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

- Dropped dead dependencies from `pyproject.toml`:
  `pydantic-settings` (never imported), the duplicate `pydantic[email]`
  pin (the `email` extra and `EmailStr` are unused), and the `stint[jira]`
  optional extra (the official `jira` SDK is never imported — the client
  is built on `httpx`). Installed environments shrink accordingly with no
  behavior change.
- Collapses the Jira dialect layer to a single `JiraCloudDialect`
  class in `stint/dialects/jira/cloud.py` and deletes the legacy
  `JiraDialectBase` (`_base.py`). DC-specific ClassVar defaults
  (`/rest/api/2`, `expected_deployment_type="Server"`, etc.) are gone;
  Cloud's overridden behavior is now the only behavior. The DC vs
  Cloud split in `paginate()`, `add_custom_field_option`,
  `_reflect_field_options`, project CRUD, and search has been folded
  into the single class. The README + `common.py` docstrings stop
  mentioning DC. (Closes DC-B in `DC_ISSUE_DRAFT.md`.)
- Removes the remaining Data Center references from user-facing and
  internal documentation: CLI auth help, package/dialect/auth docstrings,
  snapshot and schema comments, and test fixtures now describe Jira Cloud
  only. No runtime behavior change; `detect()` still rejects non-Cloud
  deployments.

## [0.4.0] - 2026-07-31

### Added

- Ten more standard Jira custom field types in `stint.fields`, closing the
  gap between stint's declarative model and Jira's built-in field type
  catalog: `RadioButtonsField`, `CheckboxesField`, `LabelsField`,
  `URLField`, `VersionField`, `MultiVersionField`, `GroupField`,
  `MultiGroupField`, `MultiUserField`, `ReadOnlyField`. Each is wired
  through schema validation (`CustomField` options rules,
  Literal-vs-options checks for the option-style types), write payloads,
  read hydration, and autogenerate's reflect-back type lookup. (Closes
  #15.)
- Experimental support for Atlassian Data Format (ADF) rich text fields in
  `stint.fields.ADFTextField`. This field type enables storage and retrieval
  of ADF-formatted content in Jira custom fields, supporting complex formatting
  including inline styles, block types, and embedded content. (Closes #16.)

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

[Unreleased]: https://github.com/curtis-turner/stint/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/curtis-turner/stint/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/curtis-turner/stint/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/curtis-turner/stint/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/curtis-turner/stint/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/curtis-turner/stint/compare/0.3.0...v0.4.0
[0.3.0]: https://github.com/curtis-turner/stint/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/curtis-turner/stint/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/curtis-turner/stint/compare/v0.1.0a2...0.1.0
[0.1.0a2]: https://github.com/curtis-turner/stint/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/curtis-turner/stint/releases/tag/v0.1.0a1
[0.1.0a0]: https://github.com/curtis-turner/stint/releases/tag/v0.1.0a0
