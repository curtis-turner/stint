# Jira Automation — Rule management REST API research

Date: 2026-11-25. Sources: the official Automation REST API reference and
its OpenAPI spec, plus Atlassian KB articles that carry full import-ready
rule JSON (real exported payloads — the most authoritative public source
for component shapes, which the OpenAPI spec types only generically).

Primary source URLs (cited as [intro], [rules], [changelog], [kb-log],
[kb-comment], [kb-sync], [smartvals]):

- [intro] https://developer.atlassian.com/cloud/automation/rest/intro/
- [rules] https://developer.atlassian.com/cloud/automation/rest/api-group-rule-management/
- [changelog] https://developer.atlassian.com/cloud/automation/api/changelog/
- OpenAPI spec: https://dac-static.atlassian.com/cloud/automation/swagger.v3.json?_v=1.0.23
- [kb-log] https://support.atlassian.com/automation/kb/track-every-change-made-to-a-custom-field-on-an-issue-in-one-location/
- [kb-comment] https://support.atlassian.com/automation/kb/automation-to-search-some-phrases-in-the-comments-body/
- [kb-sync] https://scriptrunnerhq.com/inspiration/blog/how-to-sync-groups-with-the-team-field (full export with `jira.user.condition`)
- [smartvals] https://support.atlassian.com/cloud-automation/docs/jira-smart-values-issues/

## 0. Service facts

- GA 29 August 2025 (experimental 16 June 2025); `DELETE rule` added 3
  March 2026. From **8 December 2026** request paths must match the
  documented paths exactly or the API answers 403 ([changelog]).
- Base URL: `https://api.atlassian.com/automation/public/jira/{cloudId}/rest/v1/`.
  This is a different host from the tenant; stint resolves the cloudId
  once via `GET /_edge/tenant_info` (same host as the tenant, path-only —
  the call the TMP dialect already makes) and keeps it in the state file.
- **Auth: API token or browser session only.** Forge and OAuth-2.0 apps
  cannot access any rule-management endpoint ([rules]). stint's
  `APITokenAuth` (HTTP basic email:token) works — the same credential
  works across `api.atlassian.com`.
- Authorization level for managing rules is not documented; community
  reports say Jira admin. stint probes with the cheapest read
  (`GET rule/summary?limit=1`) and treats 403 as "cannot manage rules"
  before writing anything.
- Pagination: cursor-based. `links.next` carries the next page's *query
  string* (`?cursor=...&limit=...`) — the absolute-link form was
  deprecated 1 August 2025 ([rules]).
- Error body: `{"errors": [{id, status, code, title, field}]}`.

## 1. Endpoints (paths verbatim — see the 8 Dec 2026 note)

| Operation | Path (under the base URL) | Notes |
|---|---|---|
| List summaries | `GET rule/summary` | `cursor`, `limit` query params |
| Search summaries | `POST rule/summary` | body filters: `trigger`, `state`, `scope` (single ARI), `author`, `limit`, `cursor` |
| Read a rule | `GET rule/{ruleUuid}` | `?redactSensitiveFields=` optional |
| Create a rule | `POST rule` | body `{"rule": <payload>}` (+optional `connections`); 201 `{ruleUuid}`; a caller-supplied uuid must be V7 |
| Update a rule | `PUT rule/{ruleUuid}` | same body shape as the GET response; component ids required only for pre-existing components (new created / removed deleted) |
| Enable/disable | `PUT rule/{ruleUuid}/state` | body `{"value": "ENABLED"\|"DISABLED"}` |
| Change scope | `PUT rule/{ruleUuid}/rule-scope` | body `{"ruleScopeARIs": ["ari:cloud:jira:<cloudId>:project/<id>", ...]}` — multi-project scope is one rule, not copies |
| Delete | `DELETE rule/{ruleUuid}` | **disabled rules only**; 200 on success |

The rule payload (`CreateRulePayload` / `RuleConfigResponse.rule`):
`actor {actor, type: "ACCOUNT_ID"}`, `authorAccountId`,
`canOtherRuleTrigger`, `collaborators`, `components[]`, `description`,
`labels[]`, `name`, `notifyOnError` ("FIRSTERROR"), `ruleScopeARIs[]`,
`state`, `trigger`, `uuid`, `writeAccessType` ("OWNER_ONLY" |
"UNRESTRICTED"). Export/import JSON files wrap rules as
`{"cloud": true, "rules": [...]}` and spell the scope
`ruleScope: {resources: [...]}` — the REST payload uses `ruleScopeARIs`.
`projects: []` and `ruleHome`/`billingType`/`tags` are server-side
export noise, not create-payload fields.

## 2. Component shapes (verified live on 2026-11-25 + Atlassian KB exports)

Components: `{id?, component: TRIGGER|CONDITION|CONDITION_BLOCK|ACTION,
parentId, conditionParentId, schemaVersion, type, value, children[],
conditions[], connectionId}`. `value` may be an object or a JSON string
(both accepted; the echo returns objects). Live-verified on
cumulusec.atlassian.net (build 1001.0.0-SNAPSHOT): trigger, log, comment,
edit, user-condition components all create, echo verbatim, update via PUT,
and delete.

- **Field value changed** trigger ([kb-log], [kb-sync]; live-verified):
  `type: "jira.issue.field.changed"`, schemaVersion 2,
  `value: {"changeType": "ANY_CHANGE", "fields": [{"value": "<fieldId>",
  "type": "field"}], "actions": ["edit", "transition", "assign"]}`.
  `fields[].type` may be `"fieldName"` (display name) or `"field"` (id);
  `actions` is the operation filter ("For: Edit issue, ..."). stint
  generates ids and `["edit"]` (create stays allowed). **The server
  injects `eventFilters` (= the scope ARIs) into the trigger value on
  create — server-owned, the diff normalizer drops it.**
- **User condition** (live-verified): `type: "jira.user.condition"`,
  **schemaVersion 4**,
  `value: {"conditions": [{"field": "initiator", "check": "<CHECK>",
  "criteria": [{"type": "ID", "value": "<id>"}]}], "operator": "OR"}`.
  Live: `field: "initiator"` ("User who triggered the event"), checks
  `USER_IS_NOT` (users; criteria = accountIds), `NOT_IN_GROUP` (groups;
  group ids), `NOT_IN_ROLE` (roles; role ids) — all echoed verbatim.
  **One check per component**: mixing two checks in one component's
  `conditions` list 500s (empty body); chained components AND implicitly.
  Multiple criteria within one check = OR (any of). schemaVersion 6 with
  criteria typed `ACCOUNT_ID`/`NAME` (older exports) 500s on this build;
  criteria typed `ID` is the accepted form.
- **Edit issue** action ([kb-log], [kb-sync]; live-verified):
  `type: "jira.issue.edit"`, schemaVersion 12,
  `value: {"operations": [...], "advancedFields": "<json string>",
  "sendNotifications": bool}`. `advancedFields` carries the
  `{"fields": {...}}` edit JSON as a string; the changelog-restore smart
  value round-trips verbatim; `sendNotifications: false` is how the KB
  mutes watcher notifications.
- **Comment** action ([kb-comment], [kb-sync]; live-verified):
  `type: "jira.issue.comment"`, schemaVersion 2,
  `value: {"comment": "...", "publicComment": false,
  "commentVisibility": null, "sendNotifications": true,
  "addCommentOnce": false}`.
- **Blocks** ([kb-sync]): a container `component: "CONDITION"` with
  `type: "jira.condition.container.block"`, `value: null`, children =
  `component: "CONDITION_BLOCK"`, `type: "jira.condition.if.block"`,
  `value: {"conditionMatchType": "ALL"}` — an if/elseif/else chain.
  (stint's guard avoids the block: chained flat conditions suffice.)
- Others seen in exports: `codebarrel.action.log` (smart-value string as
  value; live-verified), `jira.issue.refresh.issue` (re-fetch),
  `jira.create.variable`, `jira.create.mapping-variable`,
  `jira.issue.assign`, `jira.publish.event`. The echo nulls `schemaVersion`
  and omits/nulls `children` — server-owned, dropped by the diff
  normalizer.

## 2b. Create-payload requirements (measured live, 2026-11-25)

The generic `"The request body could not be parsed, please ensure the
values provided are valid."` 400 means **a required field is missing**,
not a wrong value (a wrong value gets a precise message). Required on
this build: `actor`, `authorAccountId`, `canOtherRuleTrigger`,
`collaborators`, `components` (>= 1; "Automation rule must contain at
least one valid condition or action."), `name`, `notifyOnError`, `state`,
`trigger`, `writeAccessType`, `ruleScopeARIs` ("The space scope for this
rule is invalid" when missing), `uuid`. Optional: `description`, `labels`.

Quirks measured live:

- `uuid` must be a client-minted RFC 9562 **UUIDv7** (Python 3.14+
  `uuid.uuid7`; stint hand-rolls it on older interpreters).
- `authorAccountId` = the authenticated account's id (the schema does not
  mark it required; the API refuses its absence with the generic 400).
- `actor` shape in the REST payload is `{"type": "ACCOUNT_ID", "actor":
  "<accountId>"}` — the id under the **`actor`** key. The export file's
  `{"type": "ACCOUNT_ID", "value": "<id>"}` shape reads as a missing
  actor ("Failed to create rule"). The default actor is the
  "Automation for Jira" app account: find it by
  `accountType == "app"` + display name (never the `557058:` id prefix —
  it misses 94% of app accounts and matches real humans).
- `state` is honored on create (ENABLED and DISABLED both) — a rule goes
  live the moment a create with `ENABLED` succeeds.
- **`POST /rule` intermittently answers 400 "Failed to create rule"** for
  identical valid payloads and then accepts a retry. stint retries that
  exact marker (2s/4s backoff, twice) and re-raises anything else.
- `DELETE rule/{uuid}` answers **415 without a Content-Type header** even
  though the request has no body; enabled rules answer 400 "Rule cannot
  be deleted unless it is already disabled."
- The trigger's `eventFilters` are rewritten from `ruleScopeARIs`
  silently on create (201, no warning) — a scope-vs-trigger disagreement
  is impossible after the fact, and a diff comparing the trigger filters
  proves nothing.
- Duplicate rule names are legal (201 for byte-identical names) —
  name-based idempotency is a stint-level convention, not API-enforced.

## 3. Revert smart values (live-measured 2026-11-25)

- On the **Field value changed** trigger the `changelog` smart value is
  **EMPTY** — the change arrives as `fieldChange`:
  `{{fieldChange.from}}` = the raw previous value (select: option id,
  `10165`-style), `{{fieldChange.fromString}}` = display text (S1),
  `{{fieldChange.toString}}` = the new display value. The documented
  changelog iteration (`{{#changelog.<field>}}{{from}}{{/}}`) evaluates
  empty on this trigger (measured in a live comment), so `fieldChange` is
  the only revert source — and it holds only the FIRST changed value:
  **an edit that changes two guarded fields at once restores only the
  first** (the trigger's own limit, documented, not solved).
- `{{fieldChange.from}}` is the RAW previous value exactly as the request
  asks: select restore = option id, labels = the previous set,
  user = the accountId.

## 3b. The edit action's smart-value forms (harvest + probes, 2026-11-25)

The Edit issue action accepts its field writes through two forms, and on
this build (1001.0.0-SNAPSHOT) a smart value only runs in one of them —
measured across seven live rules:

- **`advancedFields` (JSON string) with a smart value → silently dead.**
  `{{fieldChange.from}}` / `{{fieldChange.fromString}}` /
  `{{issue.summary}}` in any position: the rule saves ENABLED, the edit
  does nothing, and other actions in the same rule also stop (a marker
  comment after the edit never fires). Literal `advancedFields`
  (`{"fields": {field: {"value": "S1"}}}`) runs and restores in ~6s
  (changelog entries authored by "Automation for Jira").
- **`operations` with a plain-string smart value → rejected at create**:
  `"Error parsing template: Cannot construct instance of
  com.codebarrel.automation.api.component.SimpleValue ... no
  String-argument constructor ... ('{{fieldChange.fromString}}')"`.
- **`operations` with SimpleValue `{"type": "ID", "value":
  "{{fieldChange.from}}"}` → silently dead** (Jira reads the string as a
  literal option id, matches nothing, and the whole run stops).
- **`operations` with SimpleValue `{"type": "SMART", "value":
  "{{fieldChange.from}}"}` and the FIELD REFERENCED BY ID
  (`{"field": {"type": "ID", "value": "customfield_10320"}}`) → WORKS.**
  Verified live twice-clean (2026-11-25): the select restore landed in
  ~2s, authored by "Automation for Jira" in the issue changelog, with
  later actions in the rule (the comment) still firing.

**The NAME field reference (the form the Automation UI itself writes)
is a trap**: the harvest tenant carries three select fields all named
"Severity" (template leftovers `customfield_10108`/`customfield_10041`/
`customfield_10320`) and two "Root Cause" text fields. NAME-based edits
fail at runtime with `"We encountered multiple fields with the same
name and type: Severity (...); No fields or field values to edit for
work items"` — the rule runs (the comment action fires) but the edit
never lands. A UI-built guard rule on such a tenant cannot work at all;
the UI has no way to write the id form. stint references the field id
everywhere (trigger and operations), so duplicates are irrelevant. The
UI's trigger uses `{"type": "fieldName", ...}` (fires on any of the
same-named fields); stint's `{"type": "field", "value": <id>}` form is
exact.

Methodology note from the harvest: a concurrent human edit on the same
issue produced a false-positive revert (the value returned to the
original while the rule's edit was failing). A revert only counts when
the issue changelog carries the restore **authored by the Automation
actor** (`557058:...`); poll value + comments AND verify the author.

Other deltas between the UI-built rule and stint's generated payload
(stint choices verified live; kept deliberately):

- the UI's user condition is schemaVersion **6** (stint sends 4 — both
  create and echo cleanly with ID criteria; the earlier 500 was criteria
  typed ACCOUNT_ID/NAME, not the version);
- the UI rule is `writeAccessType: "OWNER_ONLY"` and its comment has
  `sendNotifications: true` (stint sends UNRESTRICTED / false).

Condition semantics (pinned 2026-11-26, from the harvest's audit log):
the user condition STOPS the rule before any action — an edit by an
allowed writer produces no comment, no revert, and (in the UI audit
log) a run that ends at the condition. The harvest's early confusion
("the rule didn't trigger") was this working by design: the UI-built
rule excludes the primary account, so trips made with it are correctly
ignored. A silent no-action on an allowed-writer edit is the guard
WORKING; verify through the audit log's run entry, not the issue.

## 4. stint wiring notes

- `cloudId` resolution: `GET /_edge/tenant_info` → `{"cloudId": ...}`
  (same response the TMP dialect parses). Cached on the dialect instance
  and persisted in the state file root (`cloud_id:`).
- Project ARIs: `ari:cloud:jira:{cloudId}:project/{projectId}` from the
  state file's project ids. Site-scoped exports show
  `ari:cloud:jira::site/<siteId>` (empty cloudId segment); stint only
  generates project scopes.
- Reflect cost: one summary page-loop + one GET per rule. stint only
  reflects rules when the schema declares any (the same laziness that
  keeps `engine.check` proportional to the project).
- Diff normalization drops: `id` (top-level and per-component, replaced
  by positions), `created`, `updated`, `uuid`, `tags` (IS_RULE_UPDATED),
  `ruleHome`, `billingType`, `authorAccountId`, `collaborators`,
  `state` (own op), `ruleScopeARIs` (own op), `projects` (export noise),
  `schemaVersion` (the echo nulls it), `eventFilters` (scope-derived),
  `actor` for guards, and defaults absent `children`/`conditions` to `[]`
  (the echo omits or nulls them where create sends `[]`).

## 5. Live-gate verdict (2026-11-25, scripts/live_gate_021.py)

- **Phase 0 (read)**: PASS — cloudId, permission probe, listing.
- **Phase 1 (round-trip)**: PASS — create DISABLED → GET echo (name,
  trigger type, components) → state flip ENABLED/DISABLED → delete. The
  diff normalizer reports declared == reflected on the real echo.
- **Phase 2 (guard shape)**: PASS — the generated ControlFieldGuard
  payload (trigger + user condition + edit + comment) creates, echoes
  verbatim (user condition: initiator / USER_IS_NOT / criteria type ID),
  and survives a PUT of the same payload with a zero diff after.
- **Phase 3 (live revert)**: **PASS** (after the id-form fix from the
  harvest, see section 3b) — the generated guard, ENABLED with an
  empty allow-list, restored a violating select write in ~2s, the
  changelog entry authored by "Automation for Jira", and the guard's
  comment fired. detect ✓ comment ✓ revert ✓.
