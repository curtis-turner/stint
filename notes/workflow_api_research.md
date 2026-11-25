# Jira Cloud REST API v3 — Workflow / Status / WorkflowScheme research

Date: 2026-11-24. Sources are the official `developer.atlassian.com` REST API v3 reference pages and the
official OpenAPI spec linked from those pages (`https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json?_v=1.8516.166`,
fetched 2026-11-24). Field requiredness (`*`) below is taken from the spec's `required` arrays; example
payloads are taken verbatim from the doc pages.

Primary source URLs used (cited as [workflows], [wfschemes], [status], [wfstatuses], [tasks], [intro], [assoc]):

- [workflows] https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-workflows/
- [wfschemes] https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-workflow-schemes/
- [status] https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-status/
- [wfstatuses] https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-workflow-statuses/
- [tasks] https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-tasks/
- [intro] https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/ (pagination + async-operations sections)
- [assoc] https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-workflow-scheme-project-associations/
- OpenAPI spec: https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json?_v=1.8516.166

Notation: fields marked `*` are `required` in the OpenAPI schema. All shapes are from the v3 spec unless noted.

---

## 0. The big picture (what is current vs deprecated as of Nov 2026)

Two generations of workflow APIs coexist in the v3 reference:

Current ("bulk"/new-model) APIs — what the library should target:

| Operation | Path |
|---|---|
| Search workflows | `GET /rest/api/3/workflows/search` |
| Bulk get workflows | `POST /rest/api/3/workflows` |
| Bulk create workflows | `POST /rest/api/3/workflows/create` |
| Validate create | `POST /rest/api/3/workflows/create/validation` |
| Bulk update workflows | `POST /rest/api/3/workflows/update` |
| Validate update | `POST /rest/api/3/workflows/update/validation` |
| Copy workflow | `POST /rest/api/3/workflows/copy` |
| Preview workflow | `POST /rest/api/3/workflows/preview` |
| Workflow capabilities (rule catalog) | `GET /rest/api/3/workflows/capabilities` |
| Delete inactive workflow | `DELETE /rest/api/3/workflow/{entityId}` |
| Workflow usage queries | `GET /rest/api/3/workflow/{workflowId}/projectUsages`, `/workflow/{workflowId}/workflowSchemes`, `/workflow/{workflowId}/project/{projectId}/issueTypeUsages` |
| Workflow version history | `POST /rest/api/3/workflow/history`, `POST /rest/api/3/workflow/history/list` |
| Statuses CRUD | `GET/PUT/POST/DELETE /rest/api/3/statuses`, `GET /rest/api/3/statuses/search`, `GET /rest/api/3/statuses/byNames`, usage endpoints |
| Workflow schemes (classic, scheme-by-id) | `GET/POST /rest/api/3/workflowscheme`, `GET/PUT/DELETE /rest/api/3/workflowscheme/{id}`, `/default`, `/issuetype/{issueType}`, `/workflow`, `/draft/*` |
| Workflow schemes (new, bulk) | `POST /rest/api/3/workflowscheme/read`, `POST /rest/api/3/workflowscheme/update`, `POST /rest/api/3/workflowscheme/update/mappings` |
| Scheme↔project | `GET/PUT /rest/api/3/workflowscheme/project`, `POST /rest/api/3/workflowscheme/project/switch` |
| Async task polling | `GET /rest/api/3/task/{taskId}`, `POST /rest/api/3/task/{taskId}/cancel` |

Deprecated / must NOT be used:

- `GET /rest/api/3/workflow/search` ("Get workflows paginated") — doc says it "will be removed on
  June 1, 2026" per [changelog CHANGE-2569]; use `GET /rest/api/3/workflows/search` instead.
  [workflows] As of Nov 2026 the removal date has passed; treat this endpoint as gone. Its response
  shape (e.g. `conditionsTree` with `PermissionCondition` nodes) is the legacy model — do not model
  against it (see §1.4).
- The `draft` field in the `WorkflowId` request object of `/rest/api/3/workflow/rule/config` and
  `/workflow/rule/config/delete` "is deprecated and will be removed from this API on November 2, 2026"
  [changelog CHANGE-3147]. (These rule-config endpoints are Connect/Forge-app-only anyway.)
- The new-model workflow APIs (`/workflows/create`, `/workflows/update`, `/workflows`, `/workflows/search`,
  `/workflows/capabilities`, `/workflows/create/validation`, `/workflows/update/validation`, `/workflows/copy`)
  all say "Connect apps cannot access this REST resource" — OAuth 2.0 (3LO) / Forge / basic auth only. [workflows]

Scopes (OAuth 2.0): workflow create/update/copy/capabilities: classic `manage:jira-configuration`,
granular `write:workflow:jira`; workflow search/read: `read:workflow:jira`. Statuses create/update/delete:
`write:workflow:jira`; read/search: `read:workflow:jira`. Schemes read: `read:workflow-scheme:jira`;
write: `write:workflow-scheme:jira` (project switch adds `write:project:jira`). [workflows] [status] [wfschemes]

---

## 1. Workflow search and the response model

### 1.1 `GET /rest/api/3/workflows/search` (current "Search workflows")

Query parameters (none are required) [workflows], OpenAPI `searchWorkflows`:

| Param | Type | Notes |
|---|---|---|
| `startAt` | integer | page offset |
| `maxResults` | integer | page size |
| `expand` | string | comma-separated expand options (doc page lists options under "expand") |
| `queryString` | string | case-insensitive partial match on workflow name |
| `orderBy` | string | `name`, `created`, `updated` (ordering prefix docs at [intro]) |
| `scope` | string | `GLOBAL` (company-managed) or `PROJECT` (team-managed) |
| `isActive` | boolean | filter active/inactive |
| `projectId` | integer | only workflows associated with the project |

Permissions: *Administer Jira* global, or at least one of *Administer projects* / *View (read-only)
workflow* project permission for project-scoped workflows. [workflows]

### 1.2 Response: `WorkflowSearchResponse` [workflows]

```jsonc
{
  "isLast": false,            // boolean
  "maxResults": 50,           // integer
  "nextPage": "<url>",        // string, url of next page if any
  "self": "<url>",            // string, url of this page
  "startAt": 0,               // integer
  "total": 5,                 // integer
  "values": [ /* JiraWorkflow[] */ ],
  "statuses": [ /* JiraWorkflowStatus[] — statuses related to the workflows, flattened */ ]
}
```

`JiraWorkflowStatus` (the tenant-level status object used across the new-model APIs):

```jsonc
{
  "id": "10001",              // string, numeric status id
  "name": "To Do",
  "description": "",
  "statusCategory": "TODO",   // enum TODO | IN_PROGRESS | DONE
  "statusReference": "10001", // string, same as id once persisted
  "scope": { "type": "GLOBAL" }  // WorkflowScope; GLOBAL or PROJECT + { project: { id } }
}
```

`JiraWorkflow`:

```jsonc
{
  "id": "c5ef565c-1b1e-427e-bc3b-e677b0dc027c",   // string UUID (new-model workflow id)
  "name": "Example Workflow",
  "description": "",
  "scope": { "type": "GLOBAL" },                  // or { "type": "PROJECT", "project": { "id": "10000" } }
  "created": "...", "updated": "...",             // strings
  "isEditable": true,                             // boolean
  "startPointLayout": { "x": 20, "y": 40 },       // WorkflowLayout {x, y}
  "loopedTransitionContainerLayout": { "x": 0, "y": 0 },
  "version": { "id": "93a7cb12-...", "versionNumber": 4 },   // DocumentVersion — optimistic concurrency
  "taskId": "<string>",            // present if an async operation is running for this workflow
  "statuses": [ /* WorkflowReferenceStatus[] */ ],
  "transitions": [ /* WorkflowTransitions[] */ ]
}
```

`WorkflowReferenceStatus` (status *inside* a workflow, referenced by `statusReference`):

```jsonc
{
  "statusReference": "10001",   // * links to JiraWorkflowStatus entries
  "deprecated": false,          // boolean
  "layout": { "x": 10, "y": 20 },
  "properties": { },            // map<string,string>
  "approvalConfiguration": { }  // JSM approvals only
}
```

### 1.3 Transitions in the response (`WorkflowTransitions`) [workflows]

```jsonc
{
  "id": "31",                          // string
  "name": "Done",
  "description": "",
  "type": "DIRECTED",                  // enum INITIAL | GLOBAL | DIRECTED
  "toStatusReference": "10003",        // destination status reference
  "links": [                           // only meaningful for DIRECTED: source statuses
    { "fromStatusReference": "10002", "fromPort": 0, "toPort": 1 }
  ],
  "customIssueEventId": "",
  "transitionScreen": { "ruleKey": "...", "parameters": { }, "id": "..." },  // optional
  "triggers": [ { "ruleKey": "...", "parameters": { }, "id": "..." } ],
  "conditions":  { /* ConditionGroupConfiguration — see below */ },
  "validators":  [ { "ruleKey": "...", "parameters": { }, "id": "..." } ],   // WorkflowRuleConfiguration[]
  "actions":     [ { "ruleKey": "...", "parameters": { }, "id": "..." } ],   // post-functions
  "properties": { }
}
```

Semantics of transition types (derived from the request/response examples in [workflows]):
- `INITIAL`: the create-transition. Has a `toStatusReference` and NO `links` (issues enter here).
  The official create example uses id `"1"`, name `"Create"`.
- `GLOBAL`: reachable from every status; no `links` in the examples.
- `DIRECTED`: explicit `links[]` from each source status.

### 1.4 Transition conditions in a RESPONSE

Current model (`GET /rest/api/3/workflows/search` → `JiraWorkflow.transitions.conditions` =
`ConditionGroupConfiguration`) [workflows]:

```jsonc
"conditions": {
  "operation": "ALL",                  // enum ALL | ANY
  "conditions": [                      // WorkflowRuleConfiguration[]
    { "id": "<optional rule instance id>", "ruleKey": "system:restrict-issue-transition",
      "parameters": { "permissionKeys": "ADMINISTER_PROJECTS" } }
  ],
  "conditionGroups": [ /* nested ConditionGroupConfiguration, recursive */ ]
}
```

So in the **current** model a response condition is identified by `ruleKey` (e.g.
`system:restrict-issue-transition`) plus a flat `parameters` map of strings. There is no
`nodeType`/`type: "PermissionCondition"` structure.

Legacy shape (deprecated `GET /rest/api/3/workflow/search` response) — recorded here only to warn
against it [workflows]:

```jsonc
"rules": {
  "conditionsTree": {
    "nodeType": "compound",            // or "simple"
    "operator": "AND",
    "conditions": [
      { "nodeType": "simple", "type": "PermissionCondition",
        "configuration": { "permissionKey": "WORK_ON_ISSUES" } }
    ]
  },
  "validators": [ { "type": "FieldRequiredValidator", "configuration": { ... } } ],
  "postFunctions": [ { "type": "UpdateIssueStatusFunction" } ]
}
```

This `conditionsTree`/`PermissionCondition` shape appears ONLY in the deprecated endpoint (removed
June 1, 2026) and in the older `GET /workflow/search` era. The library must emit/parse the
`ConditionGroupConfiguration` shape. Ambiguity: the doc page for the new search endpoint does not
include a full worked example of `conditions` in the response; the shape is taken from the OpenAPI
schema (`ConditionGroupConfiguration`), which is unambiguous.

---

## 2. `POST /rest/api/3/workflows/create` (Bulk create workflows)

Synchronous: returns `200 OK` with the created workflows and statuses (no task id, no 202). A `409`
is returned "if another workflow configuration update task is ongoing". [workflows]

Permissions: *Administer Jira* project permission to create all (including global-scoped) workflows;
*Administer projects* to create project-scoped workflows. [workflows]

### 2.1 Request: `WorkflowCreateRequest` [workflows]

Top level:

```jsonc
{
  "scope": { "type": "GLOBAL" },                  // WorkflowScope; optional; GLOBAL or
                                                  // { "type": "PROJECT", "project": { "id": "<projectId>" } }
  "statuses": [ /* WorkflowStatusUpdate[] — NEW statuses created in this same call */ ],
  "workflows": [ /* WorkflowCreate[] */ ]
}
```

`WorkflowStatusUpdate` (status definition):

```jsonc
{
  "name": "To Do",                 // *
  "statusCategory": "TODO",        // * enum TODO | IN_PROGRESS | DONE
  "description": "",               // optional
  "id": "<existing numeric id>",   // optional — "When reusing an existing status, this field should be provided"
  "statusReference": "<uuid>"      // * caller-supplied reference; see below
}
```

`statusReference` semantics (spec description): "The reference of the status. If adding a new status
to a team-managed workflow, this must be a UUID (for company-managed a UUID is not needed)." It is a
caller-chosen correlation key: you invent a reference (the official example uses random UUIDs), use
it inside the workflow's `statuses[]`/transitions, and the response maps it back to the real numeric
status id. So "statuses[] with name, statusCategory, description" is right, but each entry also
needs `statusReference`, and the workflow links statuses by reference, not by name.

`WorkflowCreate` (per workflow):

```jsonc
{
  "name": "Software workflow 1",                 // *
  "description": "",                             // optional
  "startPointLayout": { "x": -100.0, "y": -153.0 },            // optional
  "loopedTransitionContainerLayout": { "x": 0, "y": 0 },       // optional
  "statuses": [                                  // * StatusLayoutUpdate[] — which statuses the workflow uses
    { "statusReference": "<uuid>", "layout": { "x": 115, "y": -16 }, "properties": { } }
  ],
  "transitions": [ /* TransitionUpdateDTO[] */ ] // *
}
```

`TransitionUpdateDTO` (request form):

```jsonc
{
  "id": "1",                       // optional in schema; example supplies ids (initial=1, others 11/21/31)
  "name": "Create",                // optional in schema; example always supplies it
  "description": "",
  "type": "INITIAL",               // enum INITIAL | GLOBAL | DIRECTED
  "toStatusReference": "<uuid>",   // destination status
  "links": [                       // DIRECTED only: sources
    { "fromStatusReference": "<uuid>", "fromPort": 0, "toPort": 1 }
  ],
  "conditions":  { /* ConditionGroupUpdate — see §2.2 */ },
  "validators":  [ { "ruleKey": "...", "parameters": { }, "id": "..." } ],
  "actions":     [ { "ruleKey": "...", "parameters": { }, "id": "..." } ],
  "transitionScreen": { "ruleKey": "system:transition-screen", "parameters": { } },
  "triggers": [ { "ruleKey": "system:trigger-webhook", "parameters": { } } ],
  "customIssueEventId": "",
  "properties": { }
}
```

### 2.2 `rules.conditions` in the REQUEST: `ConditionGroupUpdate` [workflows]

```jsonc
"conditions": {
  "operation": "ALL",                        // * enum ALL | ANY
  "conditions": [                            // WorkflowRuleConfiguration[]
    { "ruleKey": "system:restrict-issue-transition",
      "parameters": { "roleIds": "10002,10004" } }   // parameters: map<string,string>
  ],
  "conditionGroups": [ /* nested ConditionGroupUpdate (same shape, recursive) */ ]
}
```

Each leaf condition is a `WorkflowRuleConfiguration`: `{ ruleKey*, parameters: {string:string}, id? }`.
There is NO nested `{ type: "projectRole" | "group" | "permission" }` condition format in this API —
that is the legacy model. Restricting a transition is done with the **`system:restrict-issue-transition`**
condition rule, whose parameters aggregate the legacy conditions (from the "Get available workflow
capabilities" section of [workflows]):

```jsonc
{
  "ruleKey": "system:restrict-issue-transition",
  "parameters": {
    "accountIds": "allow-reporter,5e68ac137d64450d01a77fa0",  // comma-separated; allow-assignee / allow-reporter
                                                              // and accountIds — team-managed projects only
    "roleIds": "10002,10004",                                  // comma-separated project role IDs
    "groupIds": "703ff44a-7dc8-4f4b-9aa6-a65bf3574fa4",        // comma-separated group IDs
    "permissionKeys": "ADMINISTER_PROJECTS",                   // comma-separated Jira permission keys
    "groupCustomFields": "customfield_10028",
    "allowUserCustomFields": "customfield_10072,customfield_10144,customfield_10007",
    "denyUserCustomFields": "customfield_10107"
  }
}
```

Doc: "This rule ensures that issue transitions are restricted based on user accounts, roles, group
memberships, and permissions... This condition evaluates as `true` if any of the following criteria
is met." It aggregates legacy rules AllowOnlyAssignee, AllowOnlyReporter, InAnyProjectRoleCondition,
InProjectRoleCondition, UserInAnyGroupCondition, UserInGroupCondition, PermissionCondtion [sic],
InGroupCFCondition, UserIsInCustomFieldCondition. [workflows]

Related condition rule keys (same section of [workflows]):
- `system:restrict-from-all-users` — parameters `{ "restrictMode": "users" | "usersAndAPI" }` (blocks everyone).
- `system:check-field-value` — `{ fieldId, fieldValue, comparator (=,!=,<,<=,>,>=), comparisonType (STRING|NUMBER|DATE|DATE_WITHOUT_TIME|OPTIONID) }`.
- `system:previous-status-condition` — `{ previousStatusIds, not, mostRecentStatusOnly, includeCurrentStatus, ignoreLoopTransitions }`.
- `system:parent-or-child-blocking-condition` — `{ blocker: "CHILD", statusIds }`.
- `system:separation-of-duties` — `{ fromStatusId, toStatusId }`.
- JSM: `system:jsd-approvals-block-until-approved`, `system:jsd-approvals-block-until-rejected`, `system:block-in-progress-approval`.
- Validator rule keys: `system:check-permission-validator`, `system:parent-or-child-blocking-validator`,
  `system:previous-status-validator`, `system:validate-field-value`, `system:proforma-forms-attached`,
  `system:proforma-forms-submitted`.
- Post-function rule keys: `system:change-assignee`, `system:copy-value-from-other-field`,
  `system:update-field`, `system:trigger-webhook`, `system:trigger-agent`.
- Screen rule: `system:transition-screen`; screen helper `system:remind-people-to-update-fields`.
- Triggers: `system:development-triggers` (types like `...branch-created-trigger`).
- Connect/Forge: `connect:expression-condition` / `connect:expression-validator` / `connect:remote-workflow-function`;
  `forge:expression-condition` / `forge:expression-validator` / `forge:workflow-post-function`.

The authoritative way to discover available rules (and their parameters) per workflow is
`GET /rest/api/3/workflows/capabilities?workflowId=<id>` (or `projectId` + `issueTypeId`), response
`WorkflowCapabilities { connectRules[], editorScope, forgeRules[], projectTypes[], systemRules[{description, incompatibleRuleKeys[], isAvailableForInitialTransition, isVisible, name, ruleKey, ruleType: Condition|Validator|Function|Screen}], triggerRules[] }`. [workflows]

### 2.3 Response: `WorkflowCreateResponse` [workflows]

```jsonc
{
  "statuses": [ /* JiraWorkflowStatus[] — now with real ids */ ],
  "workflows": [ /* JiraWorkflow[] — with real id, version {id, versionNumber: 0}, isEditable */ ]
}
```

Official example response shows the top-level `statuses` returning `"id": "10003"` and
`"statusReference": "10003"` (the reference resolves to the numeric id), and the workflow returning
`"id": "b9ff2384-d3b6-4d4e-9509-3ee19f607168"`, `"isEditable": true`,
`"version": { "id": "f010ac1b-...", "versionNumber": 0 }`. [workflows]

### 2.4 Initial status / the "Create" transition

- The official create example **explicitly includes** the INITIAL transition:
  `"id": "1", "name": "Create", "type": "INITIAL", "toStatusReference": "<todo-uuid>", "links": []`,
  and the response echoes it. [workflows]
- Neither the endpoint prose nor the spec states that Jira auto-adds an initial transition, or that
  exactly one INITIAL transition is required. **Ambiguity — docs are silent.** The safe design is to
  always send exactly one INITIAL transition (id `"1"`, name `"Create"`) pointing at the designated
  start status, matching the documented example; use
  `POST /rest/api/3/workflows/create/validation` to verify a payload before sending
  (body: `{ "payload": WorkflowCreateRequest*, "validationOptions": {...} }` →
  `WorkflowValidationErrorList`, 200). [workflows]

---

## 3. `POST /rest/api/3/workflows/update` (Bulk update workflows)

Synchronous (200), 409 "if another workflow configuration update task is ongoing". Same permissions
as create. [workflows]

### 3.1 Request: `WorkflowUpdateRequest` [workflows]

```jsonc
{
  "statuses": [ /* WorkflowStatusUpdate[] — statuses to associate with the workflows (new or reused) */ ],
  "workflows": [ /* WorkflowUpdate[] */ ]
}
```

`WorkflowUpdate`:

```jsonc
{
  "id": "<workflow id>",                    // * the new-model UUID
  "version": { "id": "...", "versionNumber": 3 },   // * DocumentVersion — required (optimistic locking)
  "description": "",
  "startPointLayout": { "x": 0, "y": 0 },
  "loopedTransitionContainerLayout": { "x": 0, "y": 0 },
  "statuses": [ /* StatusLayoutUpdate[] */ ],        // *
  "transitions": [ /* TransitionUpdateDTO[] */ ],    // *
  "defaultStatusMappings": [                 // StatusMigration[] — old→new status id for issue migration
    { "oldStatusReference": "3", "newStatusReference": "10003" }
  ],
  "statusMappings": [                        // StatusMappingDTO[] — per project+issue type mappings
    { "projectId": "10001", "issueTypeId": "10000",
      "statusMigrations": [ { "oldStatusReference": "3", "newStatusReference": "10003" } ] }
  ]
}
```

Operation semantics: the payload carries the FULL `statuses[]` and `transitions[]` arrays for each
workflow (same shapes as create, with existing statuses referenced via `id`/`statusReference`). The
doc prose says only "Update workflows and related statuses"; **the docs are silent on granular
add/update/delete semantics** — there are no explicit per-transition "update/delete" operation
envelopes. Treat the arrays as declarative state (diff happens on our side), and note the response
`version`/`versionNumber` must be fed back for the next update. Also note `defaultStatusMappings`
(status ids per issue-type-agnostic default) vs `statusMappings` (per project + issue type).

Spec field description worth recording for `WorkflowStatusUpdate.statusReference` in update context:
"If adding a new status to a team-managed workflow, this must be a UUID (for company-managed a UUID
is not needed)." [workflows]

### 3.2 Response: `WorkflowUpdateResponse` [workflows]

```jsonc
{ "statuses": [ /* JiraWorkflowStatus[] */ ],
  "workflows":  [ /* JiraWorkflow[] — updated version numbers */ ],
  "taskId": "<string>" }   // present if the update triggered an asynchronous operation
```

The `taskId` field means a side migration may run async even though the HTTP call was 200-synchronous;
poll `GET /rest/api/3/task/{taskId}` if present (§7).

Pre-validation: `POST /rest/api/3/workflows/update/validation` (body `{ payload, validationOptions }` →
`WorkflowValidationErrorList`). [workflows]

---

## 4. Workflow schemes

### 4.1 Listing

- `GET /rest/api/3/workflowscheme?startAt&maxResults` — "Returns a paginated list of all workflow
  schemes, not including draft workflow schemes" → `PageBeanWorkflowScheme`
  `{ isLast, maxResults, nextPage, self, startAt, total, values: WorkflowScheme[] }`. No filter
  params beyond paging. [wfschemes]
- `POST /rest/api/3/workflowscheme/read` (new bulk read) — body
  `WorkflowSchemeReadRequest { projectIds?: string[], workflowSchemeIds?: string[] }` →
  `WorkflowSchemeReadResponse[]`. [wfschemes]

`WorkflowSchemeReadResponse` (new-model scheme; workflows referenced by ID):

```jsonc
{
  "id": "3g78dg2a-...",            // *
  "name": "Software Developer Workflow Scheme",  // *
  "description": "",               // *
  "scope": { "type": "GLOBAL" },   // *  WorkflowScope (GLOBAL or PROJECT + project id)
  "version": { "id": "...", "versionNumber": 1 },  // *
  "defaultWorkflow": { "id": "...", "name": "...", "description": "", "version": {...} },  // WorkflowMetadataRestModel
  "workflowsForIssueTypes": [      // * WorkflowMetadataAndIssueTypeRestModel[]
    { "workflow": { "id": "...", "name": "...", "description": "", "version": {...} },
      "issueTypeIds": ["10013"] }
  ],
  "taskId": "<string>"             // optional — async op in flight for this scheme
}
```

### 4.2 Create: `POST /rest/api/3/workflowscheme` → 201 `WorkflowScheme`

Payload `WorkflowScheme` (classic shape — workflows referenced by NAME): [wfschemes]

```jsonc
{
  "name": "Example workflow scheme",        // required when creating; unique; max 255 chars
  "description": "",
  "defaultWorkflow": "jira",                // workflow NAME; defaults to "Jira Workflow (jira)" if omitted
  "issueTypeMappings": { "10000": "scrum workflow", "10001": "builds workflow" },
                                            // map issueTypeId -> workflow NAME; an issue type maps to one workflow
  "updateDraftIfNeeded": true               // optional
}
```

**Correction to the assumed shape**: there is no nested
`defaultWorkflow: { workflow: ..., updateMappingIfNeeded? }` object and no `mappings: { issueType: {...} }`
wrapper in this API. `defaultWorkflow` is a plain workflow-name string; mappings are a flat
`issueTypeMappings` object. (Nested-object mapping shapes belong to the NEW scheme APIs, §4.5, and
there the key is `updateDraftIfNeeded`, not `updateMappingIfNeeded`.)

`WorkflowScheme` (response) also includes: `id` (integer), `draft` (boolean),
`issueTypes` (map id → IssueTypeDetails), `self`, and for drafts `originalDefaultWorkflow`,
`originalIssueTypeMappings`, `lastModified`, `lastModifiedUser`. [wfschemes]

### 4.3 Per-id classic operations (all company-managed; *Administer Jira* global permission) [wfschemes]

- `GET /rest/api/3/workflowscheme/{id}?returnDraftIfExists` → `WorkflowScheme`
- `PUT /rest/api/3/workflowscheme/{id}` — "Classic update workflow scheme": same `WorkflowScheme`
  body. "If the workflow scheme is active ... then a draft workflow scheme is created or updated
  instead, provided that `updateDraftIfNeeded` is set to `true`."
- `DELETE /rest/api/3/workflowscheme/{id}` — cannot delete an active scheme; 204.
- `PUT /rest/api/3/workflowscheme/{id}/default` — body `DefaultWorkflow { workflow* (workflow NAME), updateDraftIfNeeded? }`; `DELETE` on the same path removes the default mapping.
- `PUT /rest/api/3/workflowscheme/{id}/issuetype/{issueType}` — body
  `IssueTypeWorkflowMapping { issueType?, updateDraftIfNeeded?, workflow (workflow NAME) }`; `DELETE` unassigns.
- `PUT /rest/api/3/workflowscheme/{id}/workflow?workflowName=<name>` — body
  `IssueTypesWorkflowMapping { defaultMapping?, issueTypes: string[], updateDraftIfNeeded?, workflow? }`; `DELETE` unassigns issue types from the workflow.
- `GET /rest/api/3/workflowscheme/{workflowSchemeId}/projectUsages` — projects using the scheme.
- Draft scheme lifecycle: `POST /rest/api/3/workflowscheme/{id}/createdraft`, `GET/PUT/DELETE /rest/api/3/workflowscheme/{id}/draft[/default|/issuetype/{issueType}|/workflow]`, and
  `POST /rest/api/3/workflowscheme/{id}/draft/publish` — body
  `PublishDraftWorkflowScheme { statusMappings?: [ { issueTypeId*, newStatusId*, statusId* } ] }`;
  asynchronous: 204 or **303 See Other** with a task. [wfschemes]

**Correction to the assumed operations**: the sub-endpoints are PUT (not POST) for
`/default`, `/issuetype/{issueType}`, `/workflow`; there is no `POST /rest/api/3/workflowscheme/{id}/mappings`.

### 4.4 Workflow reference format across APIs

- Classic scheme APIs (`POST /workflowscheme`, `PUT /workflowscheme/{id}`, `/default`, `/issuetype/*`, `/workflow`): workflows are referenced **by name** (plain strings). [wfschemes]
- New scheme APIs (`/workflowscheme/read`, `/workflowscheme/update`, `/workflowscheme/update/mappings`) and new workflow APIs: referenced **by id** (UUID strings): `defaultWorkflowId`, `workflowId`, `newWorkflowId`, `oldWorkflowId`. [wfschemes]
- `{ workflow: { id } }` object form and `{ draft: true }`: the only remaining `{ name, draft }` object form is the `WorkflowId` used by the Connect/Forge rule-config endpoints; its `draft` flag is deprecated (removal Nov 2, 2026, CHANGE-3147). Not part of the scheme APIs. [workflows]

### 4.5 New scheme update APIs

- `POST /rest/api/3/workflowscheme/update` — "Updates company-managed and team-managed project
  workflow schemes. This API doesn't have a concept of draft, so any changes made to a workflow
  scheme are immediately available. When changing the available statuses for issue types, an
  asynchronous task migrates the issues as defined in the provided mappings."
  Body `WorkflowSchemeUpdateRequest`:

  ```jsonc
  {
    "id": "10000",                          // *
    "name": "name",                         // *
    "description": "description",           // *
    "version": { "id": "...", "versionNumber": 1 },   // * DocumentVersion
    "defaultWorkflowId": "<workflow id>",   // optional (global-scoped schemes; defaults to "jira")
    "workflowsForIssueTypes": [             // WorkflowSchemeAssociation[]
      { "workflowId": "<id>", "issueTypeIds": ["10000", "10003"] }
    ],
    "statusMappingsByWorkflows": [          // MappingsByWorkflow[]
      { "oldWorkflowId": "...", "newWorkflowId": "...",
        "statusMappings": [ { "oldStatusId": "1", "newStatusId": "2" } ] }
    ],
    "statusMappingsByIssueTypeOverride": [  // MappingsByIssueTypeOverride[] — overrides per-issue-type
      { "issueTypeId": "10001", "statusMappings": [ { "oldStatusId": "1", "newStatusId": "2" } ] }
    ]
  }
  ```

  Responses: **200** if no async task, **303** if an async migration task started, 409 on conflict. [wfschemes]

- `POST /rest/api/3/workflowscheme/update/mappings` — "Gets the required status mappings for the
  desired changes to a workflow scheme... provided per issue type and workflow." Body
  `WorkflowSchemeUpdateRequiredMappingsRequest { id*, workflowsForIssueTypes* (WorkflowSchemeAssociation[]), defaultWorkflowId? }`
  → `WorkflowSchemeUpdateRequiredMappingsResponse`:

  ```jsonc
  {
    "statusMappingsByIssueTypes": [ { "issueTypeId": "10000", "statusIds": ["10000","10001"] } ],
    "statusMappingsByWorkflows": [ { "sourceWorkflowId": "10000", "targetWorkflowId": "10001",
                                     "statusIds": ["10000","10001"] } ],
    "statuses": [ { "id": "10000", "name": "To Do", "category": "TODO" } ],
    "statusesPerWorkflow": [ { "workflowId": "10000", "statuses": ["10000","10001"],
                               "initialStatusId": "10000" } ]
  }
  ```

  [wfschemes]

---

## 5. Assigning a scheme to an existing project

**Path correction**: there is no `POST /rest/api/3/workflowscheme/project/{projectIdOrKey}` in v3.
The assignment paths are: [assoc] [wfschemes]

1. `PUT /rest/api/3/workflowscheme/project` (assign; sync; 204)
   - Body `WorkflowSchemeProjectAssociation { projectId* (string), workflowSchemeId? (string; null → assign the default scheme) }`.
   - Constraint from docs: "This operation is performed only when there are no issues in the project.
     Workflow schemes can only be assigned to classic projects." So for an existing project WITH
     issues, use `/project/switch` below.
2. `POST /rest/api/3/workflowscheme/project/switch` (Experimental; async) — the real "change scheme
   for a live project" endpoint:
   - Body `WorkflowSchemeProjectSwitchBean`:

     ```jsonc
     {
       "projectId": "10001",          // *
       "targetSchemeId": "10002",     // *
       "mappingsByIssueTypeOverride": [   // MappingsByIssueTypeOverride[]; required if any statuses
         { "issueTypeId": "10000",          // in the current scheme would no longer exist in the target
           "statusMappings": [ { "oldStatusId": "3", "newStatusId": "10003" } ] }
       ]
     }
     ```

   - Doc: "If statuses from the current workflow scheme won't exist in the target workflow scheme,
     you must provide `mappingsByIssueTypeOverride` ... Use the required workflow scheme mappings API
     (`POST /workflowscheme/update/mappings`) to determine which statuses and issue types require
     mappings." [wfschemes]
   - Response: **303 See Other** with a task (`TaskProgressBeanObject`); 409 "if a conflicting task is
     already running". Follow the `Location` header to the task. [wfschemes] [intro]
   - "Never guess a mapping" requirement: mappings are required exactly for statuses that would
     disappear — discovered via `/workflowscheme/update/mappings`, which returns the `statusIds`
     requiring mapping per issue type / per workflow. The docs do not say "mappings for ALL issue
     types"; they say mappings are required where statuses won't exist in the target scheme. [wfschemes]

Note on `GET /rest/api/3/workflowscheme/project?projectId=...` (read associations): returns
`ContainerOfWorkflowSchemeAssociations { values: [ { workflowScheme, projectIds[] } ] }`; "If the
project is associated with the Default Workflow Scheme no ID is returned" (the default scheme has no
ID). [assoc]

`GET/PUT /project/{key}/workflowscheme` does not exist in the v3 spec (the paths list contains no
such resource) — the scheme-side `/workflowscheme/project` endpoints are the current interface.

---

## 6. Status endpoints (tenant-wide)

### 6.1 `GET /rest/api/3/statuses/search` — it is a GET with query params, not a POST body [status]

Query params (all optional): `projectId` (string; "or null for global statuses"), `startAt`,
`maxResults`, `searchString` (name term), `statusCategory` (`TODO|IN_PROGRESS|DONE`),
`includeGlobalStatuses` (boolean, defaults false, "only relevant for project scoped queries").
Response `PageOfStatuses`:

```jsonc
{
  "isLast": true,
  "maxResults": 2,
  "nextPage": "https://.../rest/api/3/statuses/search?startAt=2&maxResults=2",
  "self": "https://.../rest/api/3/statuses/search?startAt=0&maxResults=2",
  "startAt": 0,
  "total": 5,
  "values": [ /* JiraStatus[] */ ]
}
```

`JiraStatus`:

```jsonc
{
  "id": "1000", "name": "Finished", "description": "The issue is resolved",
  "statusCategory": "DONE",              // enum string (unlike /status/{idOrName}, see §6.3)
  "scope": { "type": "PROJECT", "project": { "id": "1" } }   // StatusScope; GLOBAL has no project
}
```

### 6.2 Current bulk status CRUD [status]

- `GET /rest/api/3/statuses?id=10000&id=10001` — bulk get by ID (1..50 ids) → `JiraStatus[]`.
- `GET /rest/api/3/statuses/byNames?name=A&name=B&projectId=` — bulk get by name (1..50) → `JiraStatus[]`.
- `POST /rest/api/3/statuses` — create; body `StatusCreateRequest { scope*: { type*: GLOBAL|PROJECT, project?: { id* } }, statuses*: [ { name*, statusCategory*, description? } ] }` → 200 `JiraStatus[]` (with ids); 409 on conflict.
- `PUT /rest/api/3/statuses` — update; body `StatusUpdateRequest { statuses*: [ { id*, name*, statusCategory*, description? } ] }` → 204.
- `DELETE /rest/api/3/statuses?id=...` — delete by ID (1..50) → 204.
- Usage queries: `GET /rest/api/3/statuses/{statusId}/projectUsages`, `/statuses/{statusId}/workflowUsages`, `/statuses/{statusId}/project/{projectId}/issueTypeUsages` (paginated with `nextPageToken`).

There is **no** `GET /rest/api/3/statuses/{id}` single-status endpoint; use `GET /statuses?id=...` or `GET /statuses/byNames?...`.

### 6.3 `GET /rest/api/3/status/{idOrName}` and `GET /rest/api/3/status` — still current, but limited [wfstatuses]

- Both are documented and NOT marked deprecated (they live in the "Workflow statuses" group).
- `GET /rest/api/3/status/{idOrName}`: "Returns a status. The status must be associated with an active
  workflow to be returned. If a name is used on more than one status, only the status found first is
  returned. Therefore, identifying the status by its ID may be preferable." Anonymous access; *Browse
  projects* permission.
- `GET /rest/api/3/status`: all statuses associated with active workflows.
- Response `StatusDetails` differs from `JiraStatus`: `statusCategory` is a full object
  (`{ colorName, id, key, name, self }`) and there is `iconUrl`, `self`; no `scope`. [wfstatuses]
- Recommendation for the library: use the `/statuses` (Status group) endpoints for admin-level
  status management; `/status/{idOrName}` only for read-side lookups, and prefer IDs over names.

---

## 7. Async tasks

- Long-running operations are signalled per [intro]: "the operation will schedule an asynchronous task
  and return a **303 (See Other)** response, indicating the location of the queued task in the
  **`Location` header**. You can query this task to get progress updates. When the task finishes, the
  response object will contain the **`result`** field."
- So the primary signalling channel is **303 + Location header** (e.g. `POST /workflowscheme/project/switch`,
  `POST /workflowscheme/{id}/draft/publish`, `POST /workflowscheme/update` when migrating issues).
- A secondary channel: some synchronous responses embed `taskId` fields — `JiraWorkflow.taskId`
  ("If there is a current asynchronous task operation for this workflow"),
  `WorkflowUpdateResponse.taskId`, `WorkflowSchemeReadResponse.taskId` ("Indicates if there's an
  asynchronous task for this workflow scheme"). Both patterns must be handled.
- `GET /rest/api/3/task/{taskId}` → `TaskProgressBeanObject` [tasks] [wfschemes]:

  ```jsonc
  {
    "id": "<string>",             // *
    "self": "<url>",              // *
    "description": "<string>",
    "message": "<string>",        // information about progress
    "progress": 51,               // * integer, percentage complete (0-100)
    "elapsedRuntime": 48,         // * integer ms
    "status": "RUNNING",          // * enum ENQUEUED | RUNNING | COMPLETE | FAILED | CANCEL_REQUESTED | CANCELLED | DEAD
    "submitted": 50, "submittedBy": 42, "started": 48, "finished": 49, "lastUpdate": 62,  // timestamps
    "result": { }                 // present when finished; content is operation-specific
  }
  ```

- Completion: `status: "COMPLETE"` (then read `result`); failure: `status: "FAILED"` (or `DEAD`,
  `CANCELLED`). "Task details are not permanently retained. As of September 2019, details are retained
  for 14 days." [tasks]
- Also `POST /rest/api/3/task/{taskId}/cancel` to cancel. [tasks]

---

## 8. Limits / pitfalls worth encoding in the library

1. **Workflow identity**: new-model workflows are identified by a UUID `id`; names are for humans.
   Search by name via `queryString`; the deprecated name-keyed endpoints are gone. The `409` conflict
   response ("another workflow configuration update task is ongoing") applies to create, update,
   copy, status create/update — the library must treat concurrent workflow-config operations as a
   retryable conflict. [workflows] [status]
2. **Workflow name uniqueness**: not stated anywhere in the current doc pages. `409 Conflict` exists
   on create but its documented cause is the concurrent-task case. **Ambiguity — assume duplicate
   names are rejected (Jira historically enforces unique workflow names) but verify at runtime; rely
   on `POST /workflows/create/validation` for pre-flight checks.**
3. **Status name uniqueness tenant-wide**: NOT enforced per docs — `GET /rest/api/3/status/{idOrName}`
   explicitly says "If a name is used on more than one status, only the status found first is
   returned," which implies duplicate names can exist (different scopes). Library must resolve status
   names via `GET /statuses/byNames?projectId=` with the right scope, or by id. [wfstatuses] [status]
4. **Status creation**: two paths — standalone `POST /rest/api/3/statuses` (explicit scope) or inline
   via `/workflows/create` `statuses[]`. In both cases the workflow links statuses by
   `statusReference`, a caller-supplied correlation id (must be a UUID for team-managed workflows),
   resolved to real numeric ids in the response. Reusing existing statuses in create/update means
   passing their `id` in `WorkflowStatusUpdate`. [workflows] [status]
5. **Company-managed vs team-managed**: encoded in `WorkflowScope.type` — `GLOBAL` = company-managed,
   `PROJECT` = team-managed (with `project.id`). Project-scoped workflows require *Administer
   projects*; global ones require *Administer Jira*. New workflow-scheme update API covers both;
   classic scheme endpoints are company-managed only. [workflows] [wfschemes]
6. **Drafts**: the "draft" concept survives in the classic scheme APIs (active schemes can't be
   edited; edit the draft and publish via `POST /workflowscheme/{id}/draft/publish`, async) — but the
   NEW scheme API (`POST /workflowscheme/update`) has "no concept of draft" and applies immediately.
   Team-managed workflows/schemes don't use the draft mechanism. [wfschemes]
7. **Optimistic concurrency**: `DocumentVersion { id, versionNumber }` is required in
   `WorkflowUpdate` and `WorkflowSchemeUpdateRequest`; every read must be fed back on write. [workflows] [wfschemes]
8. **Deleting**: `DELETE /rest/api/3/workflow/{entityId}` fails if the workflow is active, a system
   workflow, or associated with any workflow scheme (or draft scheme). Schemes can't be deleted while
   active. `DELETE /rest/api/3/statuses` deletes by id (1..50); no usage pre-check is documented —
   check `/statuses/{statusId}/workflowUsages` first in the library. [workflows] [wfschemes] [status]
9. **Scope of the `scope` field on create**: `WorkflowCreateRequest.scope` applies to the whole
   create call (status scope + workflow scope); per the example, `statuses[]` inherit it. A GLOBAL
   status created inline is reusable across company-managed workflows. [workflows]
10. **Project assignment split**: `PUT /workflowscheme/project` only works for empty projects;
    `POST /workflowscheme/project/switch` (async 303) is required for live projects and demands
    `mappingsByIssueTypeOverride` computed from `POST /workflowscheme/update/mappings`. Classic
    projects only — team-managed projects have their own workflows and are not scheme-associated. [assoc] [wfschemes]

---

## 9. Explicit ambiguities (drive design decisions)

1. Initial-transition contract on create: the example sends one INITIAL transition explicitly; docs
   never state that one is required or that Jira auto-creates "Create". Decision needed: always send
   it ourselves (recommended), validated via `/workflows/create/validation`. [workflows]
2. Update delete semantics: `/workflows/update` takes full statuses/transitions arrays; whether an
   omitted transition/status is deleted (vs erroring, vs ignored) is undocumented. Confirm empirically
   or treat as "declarative replace" with validation pre-flight. [workflows]
3. Workflow name uniqueness and max length: undocumented on current pages. [workflows]
4. `maxResults` caps per endpoint: docs say limits "may change without notice" and to probe with a
   large value; no fixed caps are published for these endpoints. [intro]
5. `expand` options for `GET /workflows/search`: listed on the doc page's collapsed "expand" section;
   exact allowed values were not captured in this pass.
6. `POST /workflowscheme/project/switch` is labeled **Experimental** — may change without notice. [wfschemes]
7. Whether GLOBAL (company-managed) workflows can be referenced in team-managed scheme mappings (and
   vice versa) is not spelled out; the scope fields suggest strict separation.
