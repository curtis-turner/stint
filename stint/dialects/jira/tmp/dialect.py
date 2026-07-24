"""TmpDialect: the team-managed-project (TMP) write surface.

Builds incrementally across the TMP dialect build phases (see
``tmp_dialect_design.md``). Phase 2 added field create/edit/delete on the
fields gateway plus two reads (field enumeration, work-type layout). Phase 3
added work-type create/delete (simplified REST) and the layout write (full-
replacement PUT, needs the ``layoutId`` from ``read_layout``). There is no
separate work-type *update*: renaming/re-describing a work type happens
through ``write_layout``'s owner data -- in TMP the work-type edit screen IS
the issue-layout screen (see ``tmp_crud_surface.md``).

Phase 4 adds ``reflect``: resolves a project by key (cloud id, numeric
project id, and project uuid all live in different places), lists its work
types via public REST, and assembles a ``TmpSnapshot`` -- a genuine,
dialect-agnostic ``Snapshot`` plus the per-work-type layout data ``Snapshot``
has no room for. ``reflect`` here takes a required ``project_key`` and
returns ``TmpSnapshot`` rather than bare ``Snapshot``, which is a deliberate
deviation from ``BaseDialect.reflect(self) -> Snapshot``: TMP objects are
inherently project-scoped (unlike CMP's genuinely tenant-wide schemes), so a
zero-arg, whole-tenant reflect would mean discovering and reflecting every
team-managed project on the site on every call. This class still does not
satisfy ``BaseDialect`` (no ``detect``, and the ``reflect`` signature above),
so it is never registered in ``Engine``/``create_engine``'s CMP-only
dialect registry -- it is reachable only through the separate
``stint.engine.create_tmp_engine``/``TmpEngine`` pair (core wiring, done
2026-07-05; see ``tmp_dialect_design.md``).

Every shape here was captured from a live tenant and confirmed working over
stint's existing token auth (see ``tmp_internal_endpoints.md``); the query
text below is the hand-written, non-persisted form stint owns, not a replay
of a frontend persisted-query hash.

Values are rendered into the query text via ``json.dumps`` rather than naive
f-string interpolation -- GraphQL string-literal escaping is a superset-
compatible subset of JSON's, so this safely handles quotes/backslashes/
unicode in user-supplied names and descriptions without needing to know the
gateway's input-object type names (which aren't visible from any HAR capture,
since the confirmed stable path uses inline literals, not `$variables`, for
mutation inputs).

Phase 5 (``desired.py``/``state.py``/``ops.py``/``reconcile.py``, siblings of
this module) added reconcile and a TMP-local op set operating directly
against this class. Phase 6 adds ``check_capabilities`` (a read-only
pre-flight against a live tenant) and an experimental-use warning emitted on
construction. Core wiring (``stint.engine.create_tmp_engine``, `stint reflect
--dialect jira_cloud_tmp --project-key`, and ``TmpState`` persistence via
``StateFile.tmp_projects``) followed as a separate pass -- see
``tmp_dialect_design.md``'s build-status section for what that did and did
not cover (notably: no CLI apply/upgrade path, since that pipeline assumes
CmpDialect's op set throughout).
"""

from __future__ import annotations

import json
import re
import uuid
import warnings
from typing import Any

from stint.client.http import JiraHTTPClient
from stint.dialects.jira.tmp.graphql import FIELDS_GATEWAY_PATH, LAYOUT_READ_PATH, GraphQLClient
from stint.dialects.jira.tmp.internal_rest import ISSUE_LAYOUTS_PATH, SIMPLIFIED_PATH, InternalRestClient
from stint.dialects.jira.tmp.models import (
    TmpCapabilityCheck,
    TmpCapabilityReport,
    TmpField,
    TmpFieldAssociation,
    TmpFieldOption,
    TmpLayout,
    TmpLayoutItem,
    TmpLayoutOwner,
    TmpProjectContext,
    TmpSnapshot,
    TmpWorkType,
)
from stint.exceptions import StintError, TmpApiError
from stint.state.snapshot import CustomFieldSnapshot, IssueTypeSnapshot, ProjectSnapshot, ServerInfoSnapshot, Snapshot

_EXPERIMENTAL_NOTICE = (
    "TmpDialect (team-managed project support) is experimental: it drives "
    "undocumented, unsupported Atlassian internal APIs that may change or "
    "break without notice. Reachable only via stint.engine.create_tmp_engine "
    "(dialect='jira_cloud_tmp'), never through create_engine. See "
    "tmp_spike_conclusion.md for the full risk record."
)

# Public REST root. TMP is Cloud-only, so this is fixed (unlike JiraDialectBase's
# ClassVar hook for DC/Cloud divergence -- not needed here, there is no TMP-on-DC).
_API_ROOT = "/rest/api/3"

# A standard system issue-type avatar id, used as create_worktype's default.
_DEFAULT_WORKTYPE_AVATAR = 10321

# Opt-in key convention: JiraProjectFieldsPage<Op>CustomField. The gateway
# names the real key in an OptInException if a call is missing its directive
# or has the wrong one, so a stale guess here is self-correcting, not silent.
# All three confirmed live 2026-07-05.
_OPTIN_CREATE_FIELD = "JiraProjectFieldsPageCreateCustomField"
_OPTIN_EDIT_FIELD = "JiraProjectFieldsPageEditCustomField"
_OPTIN_DELETE_FIELD = "JiraProjectFieldsPageDeleteCustomField"

# jiraProjectByKey (used by enumerate_fields) gates on this header instead of
# an @optIn directive -- a second, independent opt-in mechanism, confirmed
# live 2026-07-05.
_FIELD_ENUMERATION_HEADERS = {"X-ExperimentalApi": "JiraProject"}

_SECTION_MAP = {"PRIMARY": "primary", "SECONDARY": "secondary", "CONTENT": "content"}

_ENUM_LITERAL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Trimmed from the captured `SwiftJswTmpInitial` query down to what reflect
# needs: the layout id, the work-type owner, and per-field position + config.
# Dropped: availableFieldTypes/availableItems (what CAN be added -- not needed
# to reflect current state) and the feature-flag-gated sub-selections.
_LAYOUT_READ_QUERY = """
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


def _gql_str(value: str) -> str:
    """Render a Python string as an escaped GraphQL string literal."""
    return json.dumps(value)


def _gql_or_null(value: str | None) -> str:
    return "null" if value is None else _gql_str(value)


def _gql_enum(value: str) -> str:
    """Render a bare GraphQL enum literal (unquoted), e.g. for `color`.

    Sending an enum value as a quoted string 400s with a WrongType validation
    error (confirmed live 2026-07-05) -- JSON `variables` auto-coerce, but
    inline query-text literals don't. Validated against the enum-identifier
    shape (not a fixed value list, since the full JiraOptionColorInput set
    isn't captured) to avoid splicing arbitrary text into the query.
    """
    if not _ENUM_LITERAL_RE.match(value):
        raise TmpApiError(f"{value!r} is not a valid GraphQL enum literal (expected e.g. 'ORANGE_DARKER')")
    return value


def _gql_options(options: list[TmpFieldOption]) -> str:
    """Render a full-replacement options[] list for create/edit.

    Matches the captured shape exactly: existing options carry `optionId` +
    `value` only; new options (`option_id is None`) additionally carry a
    fresh `externalUuid`; `color` is included only when set.
    """
    parts = []
    for opt in options:
        fields = [f"value: {_gql_str(opt.value)}", f"optionId: {_gql_or_null(opt.option_id)}"]
        if opt.option_id is None:
            external_uuid = opt.external_uuid or str(uuid.uuid4())
            fields.append(f"externalUuid: {_gql_str(external_uuid)}")
        if opt.color is not None:
            fields.append(f"color: {_gql_enum(opt.color)}")
        parts.append("{" + ", ".join(fields) + "}")
    return "[" + ", ".join(parts) + "]"


def _parse_field_option(node: dict[str, Any]) -> TmpFieldOption:
    color = node.get("color")
    color_key = color.get("colorKey") if isinstance(color, dict) else color
    return TmpFieldOption(
        value=str(node.get("value", "")),
        option_id=str(node["optionId"]) if node.get("optionId") is not None else None,
        color=str(color_key) if color_key else None,
    )


def _parse_field(assoc: dict[str, Any]) -> TmpField:
    f = assoc["field"]
    edges = ((assoc.get("fieldOptions") or {}).get("edges")) or []
    return TmpField(
        field_id=str(f["fieldId"]),
        name=str(f.get("name", "")),
        description=str(f.get("description", "")),
        scope=str(f.get("scope", "")),
        options=tuple(_parse_field_option(e["node"]) for e in edges),
    )


def _parse_written_layout(layout_id: str, body: dict[str, Any]) -> TmpLayout:
    """Parse an issueLayouts PUT response.

    A meaningfully different shape from the gira read response (flatter, and
    `sectionType` comes back UPPERCASE where the read's `containerType` also
    is -- both map through the same lowercase section names in TmpLayoutItem).
    There is no explicit position field on write-back either; array order is
    the position, so it's assigned from enumeration order here.
    """
    owners = body.get("owners") or []
    if not owners:
        raise TmpApiError(f"issueLayouts PUT response for {layout_id} has no owners: {body!r}")
    owner_data = owners[0].get("data") or {}
    owner = TmpLayoutOwner(
        id=str(owner_data.get("id", "")),
        name=str(owner_data.get("name", "")),
        description=str(owner_data.get("description", "")),
        avatar_id=str(owner_data.get("avatarId", "")),
        icon_url=str(owner_data.get("iconUrl", "")),
    )
    items_payload = ((body.get("issueLayoutConfig") or {}).get("items")) or []
    items = tuple(
        TmpLayoutItem(
            field_id=str(item.get("key", "")),
            key=str((item.get("data") or {}).get("key", "")),
            name=str((item.get("data") or {}).get("name", "")),
            type_key=str((item.get("data") or {}).get("type", "")),
            custom=bool((item.get("data") or {}).get("custom", False)),
            global_=bool((item.get("data") or {}).get("global", False)),
            required=bool((item.get("data") or {}).get("required", False)),
            section=str(item.get("sectionType", "")).lower(),
            position=position,
            external_uuid=str((item.get("data") or {}).get("externalUuid") or ""),
            description=str((item.get("data") or {}).get("description") or ""),
            operations=dict((item.get("data") or {}).get("operations") or {}),
            provider=dict((item.get("data") or {}).get("provider") or {}),
        )
        for position, item in enumerate(items_payload)
    )
    return TmpLayout(layout_id=layout_id, owner=owner, items=items)


class TmpDialect:
    """Team-managed-project write surface. See module docstring for phase scope."""

    name = "jira_cloud_tmp"

    def __init__(self, client: JiraHTTPClient) -> None:
        warnings.warn(_EXPERIMENTAL_NOTICE, stacklevel=2)
        self._client = client
        self._graphql = GraphQLClient(client)
        self._internal_rest = InternalRestClient(client)
        self._cloud_id: str | None = None

    # ── Fields: create / edit / delete ───────────────────────────────
    async def create_field(
        self,
        *,
        cloud_id: str,
        project_id: str,
        type_key: str,
        name: str,
        description: str = "",
        options: list[TmpFieldOption] | None = None,
    ) -> TmpField:
        """createCustomFieldInProjectAndAddToAllIssueTypes: creates a
        project-scoped field and associates it to every issue type in one call."""
        query = f"""
        mutation StintTmpFieldCreate {{
          jira {{
            createCustomFieldInProjectAndAddToAllIssueTypes(input: {{
              cloudId: {_gql_str(cloud_id)}, projectId: {_gql_str(project_id)},
              type: {_gql_str(type_key)}, name: {_gql_str(name)}, description: {_gql_str(description)},
              options: {_gql_options(options or [])}
            }}) @optIn(to: "{_OPTIN_CREATE_FIELD}") {{
              success
              fieldAssociationWithIssueTypes {{
                field {{ fieldId name description scope }}
                 fieldOptions {{ edges {{ node {{ optionId value color @optIn(to: "JiraColorfulSingleSelect")
                     {{ colorKey }} }} }} }}
              }}
            }}
          }}
        }}
        """
        data = await self._graphql.query(FIELDS_GATEWAY_PATH, query=query, operation_name="StintTmpFieldCreate")
        result = data["jira"]["createCustomFieldInProjectAndAddToAllIssueTypes"]
        if not result.get("success"):
            raise TmpApiError(f"field create for {name!r} returned success != true: {result!r}")
        return _parse_field(result["fieldAssociationWithIssueTypes"])

    async def edit_field(
        self,
        *,
        cloud_id: str,
        project_id: str,
        field_id: str,
        name: str,
        description: str = "",
        options: list[TmpFieldOption] | None = None,
    ) -> TmpField:
        """editCustomField: full-replacement rename + description + options[].

        Folds field rename/description and all option add/rename/delete into
        one declarative call -- there is no separate options surface.
        """
        query = f"""
        mutation StintTmpFieldEdit {{
          jira {{
            editCustomField(input: {{
              cloudId: {_gql_str(cloud_id)}, projectId: {_gql_str(project_id)}, fieldId: {_gql_str(field_id)},
              name: {_gql_str(name)}, description: {_gql_str(description)},
              options: {_gql_options(options or [])}
            }}) @optIn(to: "{_OPTIN_EDIT_FIELD}") {{
              success
              fieldAssociationWithIssueTypes {{
                field {{ fieldId name description scope }}
                 fieldOptions {{ edges {{ node {{ optionId value color @optIn(to: "JiraColorfulSingleSelect")
                     {{ colorKey }} }} }} }}
              }}
            }}
          }}
        }}
        """
        data = await self._graphql.query(FIELDS_GATEWAY_PATH, query=query, operation_name="StintTmpFieldEdit")
        result = data["jira"]["editCustomField"]
        if not result.get("success"):
            raise TmpApiError(f"field edit for {field_id!r} returned success != true: {result!r}")
        return _parse_field(result["fieldAssociationWithIssueTypes"])

    async def delete_field(self, *, cloud_id: str, project_id: str, field_id: str) -> None:
        """deleteCustomField."""
        query = f"""
        mutation StintTmpFieldDelete {{
          jira {{
            deleteCustomField(input: {{
              cloudId: {_gql_str(cloud_id)}, projectId: {_gql_str(project_id)}, fieldId: {_gql_str(field_id)}
            }}) @optIn(to: "{_OPTIN_DELETE_FIELD}") {{ success }}
          }}
        }}
        """
        data = await self._graphql.query(FIELDS_GATEWAY_PATH, query=query, operation_name="StintTmpFieldDelete")
        result = data["jira"]["deleteCustomField"]
        if not result.get("success"):
            raise TmpApiError(f"field delete for {field_id!r} returned success != true: {result!r}")

    # ── Reads ─────────────────────────────────────────────────────────
    async def enumerate_fields(
        self, *, cloud_id: str, project_key: str, page_size: int = 200
    ) -> list[TmpFieldAssociation]:
        """jiraProjectByKey...fieldAssociationWithIssueTypes: every field
        visible on the project, both TMP-native (scope PROJECT) and shared
        (scope GLOBAL), including select-field options (reflect needs these
        to detect option drift; the `fieldOptions` sub-selection is the same
        shape already confirmed live on create_field/edit_field's responses).

        Relay-paginated on the wire (`pageInfo.hasNextPage`), but cursor
        pagination past the first page was never exercised live, so a
        truncated result fails loud here rather than silently under-reporting
        fields to reconcile against -- raise `page_size` or add cursor
        support (with a fresh live capture to confirm the `after` argument)
        before relying on this for a project with more fields than that.
        """
        query = f"""
        query StintTmpFieldEnumeration {{
          jira {{
            jiraProjectByKey(cloudId: {_gql_str(cloud_id)}, key: {_gql_str(project_key)}) {{
              projectWithVisibleIssueTypeIds {{
                fieldAssociationWithIssueTypes(first: {int(page_size)}) {{
                  edges {{
                    node {{
                      field {{ fieldId name scope typeKey }}
                      fieldOptions {{ edges {{ node {{ optionId value color @optIn(to: "JiraColorfulSingleSelect")
                     {{ colorKey }} }} }} }}
                    }}
                  }}
                  pageInfo {{ hasNextPage }}
                }}
              }}
            }}
          }}
        }}
        """
        data = await self._graphql.query(
            FIELDS_GATEWAY_PATH,
            query=query,
            operation_name="StintTmpFieldEnumeration",
            extra_headers=_FIELD_ENUMERATION_HEADERS,
        )
        conn = data["jira"]["jiraProjectByKey"]["projectWithVisibleIssueTypeIds"]["fieldAssociationWithIssueTypes"]
        if conn["pageInfo"].get("hasNextPage"):
            raise TmpApiError(
                f"field enumeration for {project_key!r} has more than {page_size} fields; "
                "cursor pagination is not implemented -- see enumerate_fields docstring"
            )
        return [
            TmpFieldAssociation(
                field_id=str(e["node"]["field"]["fieldId"]),
                name=str(e["node"]["field"].get("name", "")),
                scope=str(e["node"]["field"].get("scope", "")),
                type_key=str(e["node"]["field"].get("typeKey", "")),
                options=tuple(
                    _parse_field_option(o["node"]) for o in ((e["node"].get("fieldOptions") or {}).get("edges") or [])
                ),
            )
            for e in conn["edges"]
        ]

    async def read_layout(self, *, project_id: str, issuetype_id: int) -> TmpLayout:
        """SwiftJswTmpInitial-equivalent read: the layoutId, work-type owner
        metadata, and per-field position + config for one issue type's layout."""
        data = await self._graphql.query(
            LAYOUT_READ_PATH,
            query=_LAYOUT_READ_QUERY,
            operation_name="StintTmpLayoutRead",
            variables={"projectId": int(project_id), "extraDefinerId": int(issuetype_id)},
        )
        cfg = data["issueLayoutConfiguration"]
        if cfg.get("__typename") != "JiraIssueLayoutConfigurationResult":
            raise TmpApiError(f"unexpected issueLayoutConfiguration typename: {cfg.get('__typename')!r}")
        result = cfg["issueLayoutResult"]

        owner_node = next(
            (e["node"]["layoutOwners"][0] for e in result["usageInfo"]["edges"] if e["currentProject"]), None
        )
        if owner_node is None:
            raise TmpApiError(f"layout {result.get('id')!r} usageInfo has no currentProject edge")
        owner = TmpLayoutOwner(
            id=str(owner_node["id"]),
            name=str(owner_node.get("name", "")),
            description=str(owner_node.get("description", "")),
            avatar_id=str(owner_node.get("avatarId", "")),
            icon_url=str(owner_node.get("iconUrl", "")),
        )

        positions: dict[str, tuple[str, int]] = {}
        for container in result["containers"]:
            section = _SECTION_MAP.get(container["containerType"])
            if section is None:
                raise TmpApiError(f"unrecognized containerType: {container['containerType']!r}")
            for node in container["items"]["nodes"]:
                if node.get("__typename") == "JiraIssueItemFieldItem":
                    positions[node["fieldItemId"]] = (section, node["containerPosition"])

        field_configs = {
            n["fieldItemId"]: n
            for n in cfg["metadata"]["configuration"]["items"]["nodes"]
            if n.get("__typename") == "JiraIssueLayoutFieldItemConfiguration"
        }

        items = tuple(
            TmpLayoutItem(
                field_id=fid,
                key=str(field_configs[fid]["key"]),
                name=str(field_configs[fid].get("name", "")),
                type_key=str(field_configs[fid].get("type", "")),
                custom=bool(field_configs[fid].get("custom", False)),
                global_=bool(field_configs[fid].get("global", False)),
                required=bool(field_configs[fid].get("required", False)),
                section=positions[fid][0],
                position=positions[fid][1],
                external_uuid=str(field_configs[fid].get("externalUuid") or ""),
                description=str(field_configs[fid].get("description") or ""),
                operations=dict(field_configs[fid].get("operations") or {}),
                provider=dict(field_configs[fid].get("provider") or {}),
            )
            for fid in sorted(positions, key=lambda fid: positions[fid][1])
            if fid in field_configs
        )
        return TmpLayout(layout_id=str(result["id"]), owner=owner, items=items)

    # ── Work types: create / delete ──────────────────────────────────
    async def create_worktype(
        self,
        *,
        project_id: str,
        project_uuid: str,
        name: str,
        description: str = "",
        avatar_id: int = _DEFAULT_WORKTYPE_AVATAR,
    ) -> TmpWorkType:
        """POST .../settings/issuetype: a project-scoped work type.

        No captured field controls hierarchy (subtask vs. standard vs. epic)
        on this endpoint -- every capture created a standard work type, so
        subtask/epic creation is not supported here pending a fresh capture.
        """
        body = {
            "projectUuid": project_uuid,
            "externalUuid": str(uuid.uuid4()),
            "name": name,
            "description": description,
            "avatarId": avatar_id,
            "properties": {},
            "context": {"issueTypeKey": "custom"},
        }
        result = await self._internal_rest.post(f"{SIMPLIFIED_PATH}/project/{project_id}/settings/issuetype", json=body)
        if not isinstance(result, dict) or "id" not in result:
            raise TmpApiError(f"work-type create for {name!r} returned no id: {result!r}")
        return TmpWorkType(
            id=str(result["id"]),
            name=str(result.get("name", name)),
            avatar_id=str(result.get("avatarId", avatar_id)),
            hierarchy_level=int(result.get("hierarchyLevel", 0)),
        )

    async def delete_worktype(self, *, project_id: str, worktype_id: str) -> None:
        """Runs the checkDelete preflight before DELETE, refusing to delete a
        work type that still has issues on it rather than doing so silently."""
        base = f"{SIMPLIFIED_PATH}/project/{project_id}/settings/issuetype"
        check = await self._internal_rest.get(f"{base}/checkDelete/{worktype_id}")
        if isinstance(check, dict) and check.get("safeToDelete") is False:
            raise TmpApiError(
                f"work type {worktype_id} is not safe to delete: {check.get('issueCount', '?')} issue(s) reference it"
            )
        await self._internal_rest.delete(f"{base}/{worktype_id}")

    # ── Layout write ──────────────────────────────────────────────────
    async def write_layout(self, *, project_id: str, issuetype_id: int, layout: TmpLayout) -> TmpLayout:
        """PUT .../issueLayouts/{layoutId}: full-replacement declarative write.

        Carries both the work-type metadata (rename/description, via
        `layout.owner`) and the complete field list (association, order,
        required flags, via `layout.items`) in one call. Order is conveyed by
        array position, not an explicit field -- items are sorted by
        `position` before being sent.
        """
        items_payload = [
            {
                "type": "FIELD",
                "sectionType": item.section,
                "key": item.key,
                "data": {
                    "key": item.key,
                    "externalUuid": item.external_uuid,
                    "name": item.name,
                    "description": item.description,
                    "type": item.type_key,
                    "custom": item.custom,
                    "global": item.global_,
                    "required": item.required,
                    "operations": item.operations,
                    "provider": item.provider,
                    "properties": {},
                },
            }
            for item in sorted(layout.items, key=lambda i: i.position)
        ]
        body = {
            "projectId": int(project_id),
            "extraDefinerId": int(issuetype_id),
            "owners": [
                {
                    "type": "ISSUE_TYPE",
                    "data": {
                        "id": layout.owner.id,
                        "name": layout.owner.name,
                        "description": layout.owner.description,
                        "avatarId": layout.owner.avatar_id,
                        "iconUrl": layout.owner.icon_url,
                    },
                }
            ],
            "issueLayoutType": "ISSUE_VIEW",
            "issueLayoutConfig": {"items": items_payload},
        }
        response = await self._internal_rest.put(f"{ISSUE_LAYOUTS_PATH}/{layout.layout_id}", json=body)
        if not isinstance(response, dict):
            raise TmpApiError(f"issueLayouts PUT for {layout.layout_id} returned unexpected body: {response!r}")
        return _parse_written_layout(layout.layout_id, response)

    # ── Reflect ─────────────────────────────────────────────────────────
    async def _server_info(self) -> ServerInfoSnapshot:
        """GET /serverInfo: style-agnostic, identical to CmpDialect's own
        version (duplicated rather than shared, to keep TmpDialect independent
        of JiraDialectBase per the isolation principle)."""
        payload = await self._client.get_json(f"{_API_ROOT}/serverInfo")
        return ServerInfoSnapshot(
            deployment_type=str(payload.get("deploymentType", "")),
            version=str(payload.get("version", "")),
            base_url=str(payload.get("baseUrl", self._client.base_url)),
        )

    async def _resolve_cloud_id(self) -> str:
        """GET /_edge/tenant_info, cached: every fields-gateway call needs it."""
        if self._cloud_id is None:
            payload = await self._client.get_json("/_edge/tenant_info")
            cloud_id = payload.get("cloudId") if isinstance(payload, dict) else None
            if not cloud_id:
                raise TmpApiError(f"/_edge/tenant_info returned no cloudId: {payload!r}")
            self._cloud_id = str(cloud_id)
        return self._cloud_id

    async def resolve_project(self, *, project_key: str) -> TmpProjectContext:
        """jira_projectByIdOrKey: resolves the numeric project id and the
        project uuid (a separate identifier space that only work-type
        *creation* needs) from a project key, and confirms the project is
        actually team-managed. No `@optIn` on this field, confirmed live."""
        cloud_id = await self._resolve_cloud_id()
        query = f"""
        query StintTmpResolveProject {{
          jira_projectByIdOrKey(cloudId: {_gql_str(cloud_id)}, idOrKey: {_gql_str(project_key)}) {{
            projectId key uuid name projectType projectStyle
          }}
        }}
        """
        data = await self._graphql.query(FIELDS_GATEWAY_PATH, query=query, operation_name="StintTmpResolveProject")
        proj = data.get("jira_projectByIdOrKey")
        if not isinstance(proj, dict) or "projectId" not in proj:
            raise TmpApiError(f"project {project_key!r} not found: {data!r}")
        style = proj.get("projectStyle")
        if style != "TEAM_MANAGED_PROJECT":
            raise TmpApiError(
                f"project {project_key!r} is {style!r}, not TEAM_MANAGED_PROJECT; "
                "the TMP dialect only supports team-managed projects"
            )
        return TmpProjectContext(
            cloud_id=cloud_id,
            project_id=str(proj["projectId"]),
            project_uuid=str(proj.get("uuid", "")),
            key=str(proj.get("key", project_key)),
            name=str(proj.get("name", "")),
        )

    async def list_worktypes(self, *, project_id: str) -> list[IssueTypeSnapshot]:
        """GET /rest/api/3/issuetype/project: this project's work types.
        Public REST, style-agnostic, already used by tools/tmp_discovery.py's
        project-context resolution."""
        raw = await self._client.get_json(f"{_API_ROOT}/issuetype/project", params={"projectId": project_id})
        if not isinstance(raw, list):
            raise TmpApiError(f"issuetype/project for {project_id!r} returned non-list: {type(raw)}")
        return [
            IssueTypeSnapshot(
                id=str(it["id"]),
                name=str(it.get("name", "")),
                description=str(it.get("description", "")),
                subtask=bool(it.get("subtask", False)),
                project_scoped=True,
            )
            for it in raw
        ]

    async def reflect(self, *, project_key: str) -> TmpSnapshot:
        """Assemble a TmpSnapshot from the project's fields, work types, and
        each work type's layout. See the module docstring for why this takes
        a required project_key and returns TmpSnapshot rather than matching
        BaseDialect.reflect's exact zero-arg, bare-Snapshot signature.
        """
        ctx = await self.resolve_project(project_key=project_key)
        server_info = await self._server_info()
        fields = await self.enumerate_fields(cloud_id=ctx.cloud_id, project_key=ctx.key)
        custom_fields = {
            f.field_id: CustomFieldSnapshot(
                id=f.field_id,
                name=f.name,
                type_id=f.type_key,
                options={o.value: o.option_id for o in f.options if o.option_id is not None},
            )
            for f in fields
            if f.scope == "PROJECT"
        }
        worktypes = await self.list_worktypes(project_id=ctx.project_id)
        layouts = {
            wt.id: await self.read_layout(project_id=ctx.project_id, issuetype_id=int(wt.id)) for wt in worktypes
        }
        snapshot = Snapshot(
            server_info=server_info,
            custom_fields=custom_fields,
            issuetypes={wt.id: wt for wt in worktypes},
            projects={ctx.key: ProjectSnapshot(id=ctx.project_id, key=ctx.key, name=ctx.name, style="next-gen")},
        )
        return TmpSnapshot(snapshot=snapshot, layouts=layouts)

    # ── Capability probe ─────────────────────────────────────────────
    async def check_capabilities(self, *, project_key: str) -> TmpCapabilityReport:
        """Read-only pre-flight: confirms auth, the fields gateway, and the
        gira layout reader still respond as expected for ``project_key``,
        before a real apply run touches a live tenant.

        Deliberately does NOT verify ``@optIn`` keys are still correct --
        that can only be confirmed by attempting an actual mutation
        (create/edit/delete), and doing that automatically against a real
        user's project on every run would be wasteful and unsafe (visible
        audit-log noise, rate-limit exposure, and for create, a real
        throwaway object left behind). If Atlassian changes a required
        ``@optIn`` key, the first real write fails loudly instead
        (``TmpApiError``, naming the correct key, per the gateway's own
        self-documenting ``OptInException``/``BetaHeaderOptInException``)
        rather than being caught here. This probe only catches the class of
        breakage visible without mutating: moved endpoints, changed response
        shapes, and auth/permission failures.
        """
        checks: list[TmpCapabilityCheck] = []

        try:
            await self._client.get_json(f"{_API_ROOT}/myself")
        except StintError as e:
            checks.append(TmpCapabilityCheck("auth", False, f"{type(e).__name__}: {e}"))
            return TmpCapabilityReport(checks=tuple(checks))
        checks.append(TmpCapabilityCheck("auth", True, "token valid"))

        try:
            ctx = await self.resolve_project(project_key=project_key)
        except StintError as e:
            checks.append(TmpCapabilityCheck("resolve_project", False, f"{type(e).__name__}: {e}"))
            return TmpCapabilityReport(checks=tuple(checks))
        checks.append(TmpCapabilityCheck("resolve_project", True, f"resolved {project_key!r} -> {ctx.project_id}"))

        try:
            await self.enumerate_fields(cloud_id=ctx.cloud_id, project_key=project_key)
            checks.append(TmpCapabilityCheck("field_enumeration", True, "fields gateway read OK"))
        except StintError as e:
            checks.append(TmpCapabilityCheck("field_enumeration", False, f"{type(e).__name__}: {e}"))

        try:
            worktypes = await self.list_worktypes(project_id=ctx.project_id)
            if worktypes:
                await self.read_layout(project_id=ctx.project_id, issuetype_id=int(worktypes[0].id))
                checks.append(TmpCapabilityCheck("layout_read", True, "gira read OK"))
            else:
                checks.append(TmpCapabilityCheck("layout_read", True, "skipped: project has no work types yet"))
        except StintError as e:
            checks.append(TmpCapabilityCheck("layout_read", False, f"{type(e).__name__}: {e}"))

        return TmpCapabilityReport(checks=tuple(checks))
