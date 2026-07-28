#!/usr/bin/env python3
"""TMP internal-API discovery + contract probe.

Executable record of the team-managed-project (TMP) reverse-engineering spike.
Every operation stint needs to drive a TMP project is encoded here as a *probe*:
the surface, endpoint, request shape, opt-in key, and an assertion on the
response. Running this against a live team-managed project verifies the internal
APIs still behave as captured, and pinpoints exactly what changed when they do.

Why this exists: TMP config is only reachable through undocumented, experimental
internal endpoints (see tmp_spike_conclusion.md). Atlassian can change them on
any frontend deploy. This probe is how a maintainer answers "did they change it,
and which call?" in one command instead of another HAR safari.

Usage:
    export STINT_USER='you@example.com'          # atlassian account email
    export STINT_TOKEN='...'                      # api token
    uv run python tools/tmp_discovery.py \
        --site https://cumulusec.atlassian.net \
        --project VM                              # a TEAM-MANAGED test project

Safety: every probe that creates an object registers its own cleanup and runs it
in a finally block, so a run leaves the project as it found it. Do NOT point this
at a production project you care about -- use a throwaway team-managed project.

Verified probes are asserted as contract tests (expected to pass). Unverified
probes carry the captured shape but are not yet confirmed as hand-written calls;
they are listed, not run, until completed from a fresh capture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

GRAPHQL_PATH = "/gateway/api/graphql"
SIMPLIFIED = "/rest/internal/simplified/1.0"
# Third internal surface: the work-type layout editor. Read is hand-written
# GraphQL (no persisted-query hash) but posted to a DIFFERENT endpoint than the
# fields gateway; write is plain internal REST. Neither uses @optIn -- that
# gating is specific to the fields surface, not layouts.
GIRA_PATH = "/rest/gira/1/"
ISSUE_LAYOUTS = "/rest/internal/1.0/issueLayouts"

# Opt-in key convention observed on the fields GraphQL surface:
#   JiraProjectFieldsPage<Op>CustomField
OPTIN_CREATE_FIELD = "JiraProjectFieldsPageCreateCustomField"
OPTIN_DELETE_FIELD = "JiraProjectFieldsPageDeleteCustomField"
# Unconfirmed: following the same convention as create/delete above. The
# gateway names the real key in an OptInException if this guess is wrong
# (see tmp_internal_endpoints.md), so a failed run here is self-correcting.
OPTIN_EDIT_FIELD = "JiraProjectFieldsPageEditCustomField"

# A standard system issue-type avatar id. Adjust if a tenant rejects it.
DEFAULT_ISSUETYPE_AVATAR = 10321
TEXTFIELD_TYPE = "com.atlassian.jira.plugin.system.customfieldtypes:textfield"
SELECT_TYPE = "com.atlassian.jira.plugin.system.customfieldtypes:select"


# ── Context / client ────────────────────────────────────────────────────────


@dataclass
class Ctx:
    """Everything a probe needs, discovered once up front."""

    client: httpx.Client
    site: str
    cloud_id: str
    project_key: str
    project_id: str
    project_uuid: str
    # cleanup callbacks, run LIFO in a finally block
    cleanups: list[Callable[[], None]] = field(default_factory=list)


def make_client(user: str, token: str) -> httpx.Client:
    return httpx.Client(
        auth=(user, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30.0,
    )


def graphql(ctx: Ctx, query: str, operation_name: str, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """POST a hand-written, named query to the gateway. Raises on transport or GraphQL
    errors. The gateway rejects unnamed operations ("must be provided ... to augment
    observability") -- operation_name must match the name given in the query text AND
    is sent as its own `operationName` body field, not just embedded in the document.

    Some beta fields gate on an `X-ExperimentalApi: <Name>` HTTP header instead of (or
    in addition to) an inline `@optIn` directive -- a second, independent opt-in
    mechanism. The gateway's `BetaHeaderOptInException` names the required header."""
    payload = {"query": query, "operationName": operation_name}
    r = ctx.client.post(f"{ctx.site}{GRAPHQL_PATH}", content=json.dumps(payload), headers=extra_headers)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise ApiChangedError(f"GraphQL errors: {json.dumps(body['errors'])[:400]}")
    return body["data"]


def gira(ctx: Ctx, query: str, operation_name: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a hand-written, named query to the gira endpoint (layout read) -- a
    separate surface from the fields gateway, with its own path and no @optIn gating."""
    payload = {"query": query, "operationName": operation_name, "variables": variables}
    r = ctx.client.post(f"{ctx.site}{GIRA_PATH}", content=json.dumps(payload))
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise ApiChangedError(f"gira GraphQL errors: {json.dumps(body['errors'])[:400]}")
    return body["data"]


class ApiChangedError(Exception):
    """Raised when a probe's response does not match the captured contract."""


# ── Discovery (cloud id, project id/uuid, style) ─────────────────────────────


def discover(client: httpx.Client, site: str, project_key: str) -> Ctx:
    # cloud id from the tenant edge endpoint
    edge = client.get(f"{site}/_edge/tenant_info")
    edge.raise_for_status()
    cloud_id = edge.json()["cloudId"]

    ctx = Ctx(client=client, site=site, cloud_id=cloud_id, project_key=project_key, project_id="", project_uuid="")

    # project id + uuid + style via a hand-written jira_projectByIdOrKey query
    q = f'''query StintDiscoverProject {{
      jira_projectByIdOrKey(cloudId: "{cloud_id}", idOrKey: "{project_key}") {{
        projectId key uuid name projectType projectStyle
      }}
    }}'''
    data = graphql(ctx, q, "StintDiscoverProject")
    proj = data["jira_projectByIdOrKey"]
    ctx.project_id = str(proj["projectId"])
    ctx.project_uuid = proj["uuid"]
    style = proj.get("projectStyle")
    if style != "TEAM_MANAGED_PROJECT":
        raise SystemExit(
            f"Project {project_key!r} is {style!r}, not TEAM_MANAGED_PROJECT. "
            f"Point --project at a team-managed project."
        )
    return ctx


def get_first_standard_issuetype(ctx: Ctx) -> tuple[int, str]:
    """A standard (non-subtask) work type id via the public REST API, to use as
    extraDefinerId for the layout probes. Prefers "Task" if present."""
    r = ctx.client.get(f"{ctx.site}/rest/api/3/issuetype/project", params={"projectId": ctx.project_id})
    r.raise_for_status()
    types = r.json()
    standard = [t for t in types if not t.get("subtask")]
    if not standard:
        raise ApiChangedError("no standard (non-subtask) issue types found on project")
    t = next((t for t in standard if t["name"] == "Task"), standard[0])
    return int(t["id"]), t["name"]


# ── Probes ───────────────────────────────────────────────────────────────────


@dataclass
class Probe:
    name: str
    surface: str  # "graphql" | "simplified-rest" | "internal-rest" | "public-rest"
    summary: str
    run: Callable[[Ctx], str]  # returns a short result string; raises on contract break
    verified: bool = True


def probe_auth_control(ctx: Ctx) -> str:
    r = ctx.client.get(f"{ctx.site}/rest/api/3/myself")
    r.raise_for_status()
    acct = r.json().get("accountId")
    if not acct:
        raise ApiChangedError("myself returned no accountId")
    return f"authed as {acct}"


def probe_field_create_delete(ctx: Ctx) -> str:
    name = f"disco-field-{uuid.uuid4().hex[:8]}"
    q = f'''mutation StintFieldCreate {{ jira {{
      createCustomFieldInProjectAndAddToAllIssueTypes(input: {{
        cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}",
        type: "{TEXTFIELD_TYPE}", name: "{name}", description: "discovery probe", options: []
      }}) @optIn(to: "{OPTIN_CREATE_FIELD}") {{
        success fieldAssociationWithIssueTypes {{ field {{ fieldId scope }} }}
      }}
    }} }}'''
    res = graphql(ctx, q, "StintFieldCreate")["jira"]["createCustomFieldInProjectAndAddToAllIssueTypes"]
    if not res.get("success"):
        raise ApiChangedError(f"create returned success != true: {res}")
    fid = res["fieldAssociationWithIssueTypes"]["field"]["fieldId"]
    scope = res["fieldAssociationWithIssueTypes"]["field"]["scope"]
    if scope != "PROJECT":
        raise ApiChangedError(f"created field scope {scope!r}, expected PROJECT")

    def cleanup() -> None:
        dq = f'''mutation StintFieldDelete {{ jira {{
          deleteCustomField(input: {{cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}", fieldId: "{fid}"}})
          @optIn(to: "{OPTIN_DELETE_FIELD}") {{ success }}
        }} }}'''
        graphql(ctx, dq, "StintFieldDelete")

    ctx.cleanups.append(cleanup)
    return f"created {fid} (scope PROJECT), delete verified via cleanup"


def probe_field_edit(ctx: Ctx) -> str:
    """editCustomField: rename + declarative options[] (add/rename/delete by array diff).

    Shape confirmed from a persisted-query HAR capture 2026-07-05 (create with one
    option, then two edits: add an option, then rename the field/description and an
    option). This probe replays that shape hand-written with an inline @optIn guess
    (JiraProjectFieldsPageEditCustomField, per the create/delete naming convention).
    If the guess is wrong the gateway's OptInException names the real key.
    """
    name = f"disco-select-{uuid.uuid4().hex[:8]}"
    create_q = f'''mutation StintFieldEditProbeCreate {{ jira {{
      createCustomFieldInProjectAndAddToAllIssueTypes(input: {{
        cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}",
        type: "{SELECT_TYPE}", name: "{name}", description: "",
        options: [{{value: "test1", optionId: null, externalUuid: "{uuid.uuid4()}"}}]
      }}) @optIn(to: "{OPTIN_CREATE_FIELD}") {{
        success
        fieldAssociationWithIssueTypes {{
          field {{ fieldId }}
          fieldOptions {{ edges {{ node {{ optionId value }} }} }}
        }}
      }}
    }} }}'''
    created = graphql(ctx, create_q, "StintFieldEditProbeCreate")["jira"][
        "createCustomFieldInProjectAndAddToAllIssueTypes"
    ]
    if not created.get("success"):
        raise ApiChangedError(f"edit-probe create returned success != true: {created}")
    fid = created["fieldAssociationWithIssueTypes"]["field"]["fieldId"]
    option_id = created["fieldAssociationWithIssueTypes"]["fieldOptions"]["edges"][0]["node"]["optionId"]

    def cleanup() -> None:
        dq = f'''mutation StintFieldEditProbeDelete {{ jira {{
          deleteCustomField(input: {{cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}", fieldId: "{fid}"}})
          @optIn(to: "{OPTIN_DELETE_FIELD}") {{ success }}
        }} }}'''
        graphql(ctx, dq, "StintFieldEditProbeDelete")

    ctx.cleanups.append(cleanup)

    new_name = f"{name}-renamed"
    edit_q = f'''mutation StintFieldEdit {{ jira {{
      editCustomField(input: {{
        cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}", fieldId: "{fid}",
        name: "{new_name}", description: "edited by discovery probe",
        options: [
          {{optionId: {option_id}, value: "test1"}},
          {{optionId: null, value: "test2", externalUuid: "{uuid.uuid4()}", color: ORANGE_DARKER}}
        ]
      }}) @optIn(to: "{OPTIN_EDIT_FIELD}") {{
        success
        fieldAssociationWithIssueTypes {{
          field {{ fieldId name description }}
          fieldOptions {{ edges {{ node {{ optionId value }} }} }}
        }}
      }}
    }} }}'''
    edited = graphql(ctx, edit_q, "StintFieldEdit")["jira"]["editCustomField"]
    if not edited.get("success"):
        raise ApiChangedError(f"edit returned success != true: {edited}")
    field = edited["fieldAssociationWithIssueTypes"]["field"]
    if field["name"] != new_name:
        raise ApiChangedError(f"edit did not apply rename: {field}")
    options = edited["fieldAssociationWithIssueTypes"]["fieldOptions"]["edges"]
    if len(options) != 2:
        raise ApiChangedError(f"edit did not apply option add, expected 2 options: {options}")
    return f"edited {fid}: renamed + description set + option added (now {len(options)} options)"


SECTION_MAP = {"PRIMARY": "primary", "SECONDARY": "secondary", "CONTENT": "content"}

# Trimmed from the captured `SwiftJswTmpInitial` query (browser HAR, 2026-07-05)
# down to what reflect actually needs: the layout id, the work-type owner (for
# the PUT's `owners[]`), and per-field position + config (for `issueLayoutConfig.
# items[]`). Dropped: availableFieldTypes/availableItems (what CAN be added --
# not needed to reflect current state) and the feature-flag-gated sub-selections.
LAYOUT_READ_QUERY = """
query StintTmpLayoutRead($projectId: Long!, $extraDefinerId: Long!) {
  issueLayoutConfiguration(issueLayoutKey: {projectId: $projectId, extraDefinerId: $extraDefinerId}, type: ISSUE_VIEW) {
    __typename
    ... on JiraIssueLayoutConfigurationResult {
      issueLayoutResult {
        id
        name
        usageInfo {
          edges {
            currentProject
            node {
              layoutOwners {
                __typename
                ... on JiraIssueLayoutIssueTypeOwner { id name description avatarId iconUrl }
              }
            }
          }
        }
        containers {
          containerType
          items { nodes { __typename ... on JiraIssueItemFieldItem { fieldItemId containerPosition } } }
        }
      }
      metadata {
        configuration {
          items {
            nodes {
              __typename
              ... on JiraIssueLayoutFieldItemConfiguration {
                fieldItemId key name type custom global required externalUuid description
                operations {
                  editable canModifyRequired canModifyOptions canModifyDefaultValue
                  canModifyPropertyConfiguration removable deletable canAssociateInSettings categoriesWhitelist
                }
                provider { key name }
              }
            }
          }
        }
      }
    }
  }
}
"""


def probe_layout_roundtrip(ctx: Ctx) -> str:
    """Read a work type's layout (gira, hand-written) then PUT it back unchanged
    (issueLayouts, internal REST) -- confirms both surfaces without changing
    anything observable.

    Shapes captured from a browser HAR 2026-07-05 (issue-type-list-update.har):
    read = query SwiftJswTmpInitial (trimmed above to what reflect needs); write
    = PUT /rest/internal/1.0/issueLayouts/{layoutId}. This is a THIRD internal
    surface (its own path prefix, `/rest/gira/1/` + `/rest/internal/1.0/`),
    distinct from both the fields gateway and the simplified-REST work-type
    endpoints, and unlike the fields surface, neither call uses @optIn.
    """
    it_id, it_name = get_first_standard_issuetype(ctx)
    data = gira(
        ctx, LAYOUT_READ_QUERY, "StintTmpLayoutRead", {"projectId": int(ctx.project_id), "extraDefinerId": it_id}
    )
    cfg = data["issueLayoutConfiguration"]
    if cfg["__typename"] != "JiraIssueLayoutConfigurationResult":
        raise ApiChangedError(f"unexpected issueLayoutConfiguration typename: {cfg['__typename']}")
    result = cfg["issueLayoutResult"]
    layout_id = result["id"]

    owner = next(e["node"]["layoutOwners"][0] for e in result["usageInfo"]["edges"] if e["currentProject"])

    positions: dict[str, tuple[str, int]] = {}
    for container in result["containers"]:
        section = SECTION_MAP.get(container["containerType"])
        if section is None:
            raise ApiChangedError(f"unrecognized containerType: {container['containerType']!r}")
        for node in container["items"]["nodes"]:
            if node["__typename"] == "JiraIssueItemFieldItem":
                positions[node["fieldItemId"]] = (section, node["containerPosition"])

    field_configs = {
        n["fieldItemId"]: n
        for n in cfg["metadata"]["configuration"]["items"]["nodes"]
        if n["__typename"] == "JiraIssueLayoutFieldItemConfiguration"
    }

    ordered_ids = sorted(positions, key=lambda fid: positions[fid][1])
    items = [
        {
            "type": "FIELD",
            "sectionType": positions[fid][0],
            "key": field_configs[fid]["key"],
            "data": {
                "key": field_configs[fid]["key"],
                "externalUuid": field_configs[fid].get("externalUuid") or "",
                "name": field_configs[fid]["name"],
                "description": field_configs[fid].get("description") or "",
                "type": field_configs[fid]["type"],
                "custom": field_configs[fid]["custom"],
                "global": field_configs[fid]["global"],
                "required": field_configs[fid]["required"],
                "operations": field_configs[fid]["operations"],
                "provider": field_configs[fid]["provider"],
                "properties": {},
            },
        }
        for fid in ordered_ids
    ]

    body = {
        "projectId": int(ctx.project_id),
        "extraDefinerId": it_id,
        "owners": [
            {
                "type": "ISSUE_TYPE",
                "data": {
                    "id": owner["id"],
                    "name": owner["name"],
                    "description": owner["description"],
                    "avatarId": owner["avatarId"],
                    "iconUrl": owner["iconUrl"],
                },
            }
        ],
        "issueLayoutType": "ISSUE_VIEW",
        "issueLayoutConfig": {"items": items},
    }

    r = ctx.client.put(
        f"{ctx.site}{ISSUE_LAYOUTS}/{layout_id}",
        content=json.dumps(body),
        headers={"X-Atlassian-Token": "no-check"},
    )
    r.raise_for_status()
    echoed_items = r.json().get("issueLayoutConfig", {}).get("items", [])
    if len(echoed_items) != len(items):
        raise ApiChangedError(f"PUT round-trip item count mismatch: sent {len(items)}, got back {len(echoed_items)}")
    return (
        f"layout {layout_id} for {it_name!r} ({it_id}): read {len(items)} items across "
        f"{len(result['containers'])} containers, PUT round-trip echoed {len(echoed_items)} back unchanged"
    )


def probe_worktype_create_delete(ctx: Ctx) -> str:
    body = {
        "projectUuid": ctx.project_uuid,
        "externalUuid": str(uuid.uuid4()),
        "name": f"disco-type-{uuid.uuid4().hex[:8]}",
        "description": "discovery probe",
        "avatarId": DEFAULT_ISSUETYPE_AVATAR,
        "properties": {},
        "context": {"issueTypeKey": "custom"},
    }
    url = f"{ctx.site}{SIMPLIFIED}/project/{ctx.project_id}/settings/issuetype"
    r = ctx.client.post(url, content=json.dumps(body), headers={"X-Atlassian-Token": "no-check"})
    r.raise_for_status()
    it_id = r.json().get("id")
    if not it_id:
        raise ApiChangedError(f"work-type create returned no id: {r.text[:200]}")

    def cleanup() -> None:
        d = ctx.client.request(
            "DELETE",
            f"{url}/{it_id}",
            headers={"X-Atlassian-Token": "no-check"},
        )
        if d.status_code not in (200, 204):
            raise ApiChangedError(f"work-type delete returned {d.status_code}")

    ctx.cleanups.append(cleanup)
    return f"created issuetype {it_id}, delete verified via cleanup"


def probe_field_enumeration(ctx: Ctx) -> str:
    """Read path: enumerate project-scoped + associated fields for reflect/diff.

    jiraProjectByKey gates on an X-ExperimentalApi HTTP header (a beta-field opt-in
    distinct from the @optIn directive used on the mutations) -- the gateway's
    BetaHeaderOptInException names it: X-ExperimentalApi: JiraProject. Deliberately
    omits `projectScopedFieldsCount` (gated behind its own @optIn, unneeded by
    reflect) rather than chasing a third opt-in for a field stint doesn't use.
    """
    q = f'''query StintFieldEnumeration {{
      jira {{
        jiraProjectByKey(cloudId: "{ctx.cloud_id}", key: "{ctx.project_key}") {{
          projectWithVisibleIssueTypeIds {{
            fieldAssociationWithIssueTypes(first: 100) {{
              edges {{ node {{ field {{ fieldId name scope typeKey }} }} }}
              pageInfo {{ hasNextPage }}
            }}
          }}
        }}
      }}
    }}'''
    data = graphql(ctx, q, "StintFieldEnumeration", extra_headers={"X-ExperimentalApi": "JiraProject"})
    edges = data["jira"]["jiraProjectByKey"]["projectWithVisibleIssueTypeIds"]["fieldAssociationWithIssueTypes"][
        "edges"
    ]
    return f"enumerated {len(edges)} field associations"


PROBES: list[Probe] = [
    Probe("auth_control", "public-rest", "GET /rest/api/3/myself -- token validity control", probe_auth_control),
    Probe(
        "field_create_delete",
        "graphql",
        "createCustomFieldInProjectAndAddToAllIssueTypes + deleteCustomField (@optIn)",
        probe_field_create_delete,
    ),
    Probe(
        "field_edit",
        "graphql",
        "editCustomField: rename + description + declarative options[] (@optIn confirmed)",
        probe_field_edit,
    ),
    Probe("worktype_create_delete", "simplified-rest", "POST/DELETE /settings/issuetype", probe_worktype_create_delete),
    Probe(
        "field_enumeration",
        "graphql",
        "read path: jiraProjectByKey.fieldAssociationWithIssueTypes (X-ExperimentalApi confirmed)",
        probe_field_enumeration,
    ),
    Probe(
        "layout_roundtrip",
        "graphql+internal-rest",
        "gira layout read + issueLayouts PUT round-trip (no @optIn on this surface)",
        probe_layout_roundtrip,
    ),
]

# All operations stint needs have now been captured (see tmp_crud_surface.md).
# layout_roundtrip confirmed live 2026-07-05 (promoted to verified=True). What
# remains: rerun field_edit and field_enumeration with --include-unverified
# after their bug fixes (enum literal, X-ExperimentalApi header) to get a clean
# pass, then promote them too.
PENDING: list[str] = []


# ── Runner ───────────────────────────────────────────────────────────────────


def run(ctx: Ctx, include_unverified: bool = False) -> int:
    results: list[tuple[str, str, str]] = []  # (name, status, detail)
    try:
        for p in PROBES:
            if not p.verified and not include_unverified:
                results.append((p.name, "SKIP", "unverified -- rerun with --include-unverified to attempt"))
                continue
            try:
                detail = p.run(ctx)
                results.append((p.name, "OK" if p.verified else "CONFIRMED", detail))
            except Exception as e:  # noqa: BLE001 -- diagnostic tool, report everything
                status = "BROKEN" if p.verified else "UNCONFIRMED"
                results.append((p.name, status, f"{type(e).__name__}: {e}"))
    finally:
        # LIFO cleanup, best-effort, report failures
        for c in reversed(ctx.cleanups):
            try:
                c()
            except Exception as e:  # noqa: BLE001
                results.append(("cleanup", "WARN", f"{type(e).__name__}: {e}"))

    broken = sum(1 for _, s, _ in results if s == "BROKEN")
    print(f"\nTMP discovery against {ctx.site} project {ctx.project_key} (id {ctx.project_id}, team-managed)\n")
    for name, status, detail in results:
        print(f"  [{status:11}] {name}: {detail}")
    if PENDING:
        print("\n  pending (no capture yet, not run):")
        for item in PENDING:
            print(f"    - {item}")
    print(f"\n{'FAIL' if broken else 'OK'}: {broken} broken of {sum(1 for p in PROBES if p.verified)} verified probes.")
    if any(s == "CONFIRMED" for _, s, _ in results):
        print("Promote CONFIRMED probes to verified=True in PROBES once satisfied they're stable.")
    return 1 if broken else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="TMP internal-API discovery probe")
    ap.add_argument("--site", default=os.environ.get("STINT_SITE"), help="e.g. https://cumulusec.atlassian.net")
    ap.add_argument("--project", default=os.environ.get("STINT_TMP_PROJECT"), help="a TEAM-MANAGED test project key")
    ap.add_argument(
        "--include-unverified",
        action="store_true",
        help="also attempt probes not yet confirmed as hand-written token calls (e.g. guessed @optIn keys)",
    )
    args = ap.parse_args()

    user, token = os.environ.get("STINT_USER"), os.environ.get("STINT_TOKEN")
    missing = [
        n
        for n, v in [
            ("STINT_USER", user),
            ("STINT_TOKEN", token),
            ("--site/STINT_SITE", args.site),
            ("--project/STINT_TMP_PROJECT", args.project),
        ]
        if not v
    ]
    if missing or user is None or token is None:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    site = args.site.rstrip("/")
    if not site.startswith(("http://", "https://")):
        site = f"https://{site}"

    with make_client(user, token) as client:
        ctx = discover(client, site, args.project)
        return run(ctx, include_unverified=args.include_unverified)


if __name__ == "__main__":
    raise SystemExit(main())
