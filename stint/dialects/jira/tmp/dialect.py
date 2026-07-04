"""TmpDialect: the team-managed-project (TMP) write surface.

Builds incrementally across the TMP dialect build phases (see
``tmp_dialect_design.md``). This phase (2) adds: field create/edit/delete on
the fields gateway, and two reads (field enumeration, work-type layout).
Work types, the layout write, ``detect``/``reflect``, and reconcile land in
later phases -- until then this class does not satisfy ``BaseDialect`` and
must not be registered as a selectable dialect.

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
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from stint.client.http import JiraHTTPClient
from stint.dialects.jira.tmp.graphql import FIELDS_GATEWAY_PATH, LAYOUT_READ_PATH, GraphQLClient
from stint.dialects.jira.tmp.models import (
    TmpField,
    TmpFieldAssociation,
    TmpFieldOption,
    TmpLayout,
    TmpLayoutItem,
    TmpLayoutOwner,
)
from stint.exceptions import TmpApiError

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


class TmpDialect:
    """Team-managed-project write surface. See module docstring for phase scope."""

    name = "jira_cloud_tmp"

    def __init__(self, client: JiraHTTPClient) -> None:
        self._client = client
        self._graphql = GraphQLClient(client)

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
                fieldOptions {{ edges {{ node {{ optionId value color {{ colorKey }} }} }} }}
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
                fieldOptions {{ edges {{ node {{ optionId value color {{ colorKey }} }} }} }}
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
        (scope GLOBAL).

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
                  edges {{ node {{ field {{ fieldId name scope typeKey }} }} }}
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
