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

## 2. Component shapes (from real Cloud exports)

Components: `{id?, component: TRIGGER|CONDITION|CONDITION_BLOCK|ACTION,
parentId, conditionParentId, schemaVersion, type, value, children[],
conditions[], connectionId}`. `value` is usually a **JSON string**; some
exports show it as an object (Jira accepts both; stint generates
objects/strings per the shapes below).

- **Field value changed** trigger ([kb-log], [kb-sync]):
  `type: "jira.issue.field.changed"`, schemaVersion 2,
  `value: {"changeType": "ANY_CHANGE", "fields": [{"value": "<fieldId>",
  "type": "field"}], "actions": ["edit", "transition", "assign"]}`.
  `fields[].type` may be `"fieldName"` (display name) or `"field"` (id);
  `actions` is the operation filter ("For: Edit issue, ..."). stint
  generates ids and `["edit"]` (create stays allowed).
- **User condition** ([kb-sync], full export):
  `type: "jira.user.condition"`, schemaVersion 6,
  `value: {"conditions": [{"field": "<user>", "check": "<CHECK>",
  "criteria": [...]}], "operator": "AND"|"OR"}`.
  Confirmed entries: `{"field": "assignee", "check": "USER_IS_NOT",
  "criteria": []}` and `{"field": "assignee", "check": "NOT_IN_GROUP",
  "criteria": [{"type": "SMART", "value": "{{GroupAssigned}}"}]}`.
  stint generates: `field: "initiator"`, checks `USER_IS_NOT` (users),
  `NOT_IN_GROUP` (groups), `NOT_IN_PROJECT_ROLE` (roles), criteria
  `{"type": "ACCOUNT_ID"|"NAME", "value": ...}`, `operator: "AND"`
  (De Morgan: the AND of negations = "not any allowed writer").
  **To verify on a live tenant first**: the initiator `field` spelling,
  the exact `NOT_IN_PROJECT_ROLE` check string, and the criteria entry
  types for literal (non-SMART) values.
- **Edit issue** action ([kb-log], [kb-sync]):
  `type: "jira.issue.edit"`, schemaVersion 12,
  `value: {"operations": [...], "advancedFields": "<json string>",
  "sendNotifications": bool}`. `advancedFields` carries the
  `{"fields": {...}}` edit JSON as a string; `sendNotifications: false`
  is how the KB mutes watcher notifications.
- **Comment** action ([kb-comment], [kb-sync]):
  `type: "jira.issue.comment"`, schemaVersion 2,
  `value: {"comment": "...", "publicComment": false,
  "commentVisibility": null, "sendNotifications": true,
  "addCommentOnce": false}`.
- **Blocks** ([kb-sync]): a container `component: "CONDITION"` with
  `type: "jira.condition.container.block"`, `value: null`, children =
  `component: "CONDITION_BLOCK"`, `type: "jira.condition.if.block"`,
  `value: {"conditionMatchType": "ALL"}` — an if/elseif/else chain.
  (stint's guard avoids the block: one flat user condition suffices.)
- Others seen in exports: `codebarrel.action.log` (smart-value string as
  value), `jira.issue.refresh.issue` (re-fetch), `jira.create.variable`,
  `jira.create.mapping-variable`, `jira.issue.assign`, `jira.publish.event`.

## 3. Revert smart values

- `{{fieldChange}}` holds only the *first* changed value; the changelog
  form per field iterates them all:
  `{{#changelog.<fieldId>}}{{from}}{{/}}` ([kb-sync] uses
  `{{#changelog.status}}{{from}}{{/}}`; [smartvals] documents
  `{{changelog.<field>}}` with `.from/.fromString/.to/.toString`).
- `from` is the raw previous value; `fromString` is display text.
  Selects restore by option id; multi-value `fromString` is fragile
  (Atlassian's own warning) — restore from `from`.
- Mention the initiator in comments with
  `[~accountid:{{initiator.accountId}}]` ([kb-sync]).
- Related: `{{deletedFieldChange.values}}` (value removed entirely),
  `{{fieldChange.toString}}`.

stint's generated restore forms (per declared field type):

| Field type | Restore value in `advancedFields.fields` |
|---|---|
| scalar (text/number/date/select/read-only/...) | `{{#changelog.<id>}}{{from}}{{/}}` |
| UserField | `{"accountId": "{{#changelog.<id>}}{{from}}{{/}}"}` |
| GroupField | `{"name": "{{#changelog.<id>}}{{from}}{{/}}"}` |
| LabelsField | `{{#changelog.<id>}}{{from.split(" ")}}{{/}}` |
| multi (MultiSelect/Checkboxes/MultiVersion/MultiGroup/MultiUser) | `{{#changelog.<id>}}{{from.split(", ")}}{{/}}` |
| ADF system text (Description) | scalar form; ADF restore is a known limit, verify live |

**To verify live per field type** (the request's acceptance bar): write as
a non-allowed identity, assert the value is back, the comment exists, and
the rule ran once. The multi-value `.split(...)` separator assumptions
(labels are space-joined; custom multis comma-joined) and the ADF case
are the first things to check; `scripts/verify_screen_lock.py` is the
template for the driver.

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
  and `actor` for guards (stint omits it; Jira reports the "Automation
  for Jira" account id).
