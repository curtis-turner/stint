"""Logic shared between the DC and Cloud Jira dialects.

Pagination, payload parsers for each admin object. The DC and Cloud dialects
build URLs against their own api_root and delegate parsing here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from stint.client.http import JiraHTTPClient
from stint.state.snapshot import (
    CustomFieldSnapshot,
    FieldConfigurationItemSnapshot,
    FieldConfigurationSchemeMappingSnapshot,
    FieldConfigurationSchemeSnapshot,
    FieldConfigurationSnapshot,
    IssueTypeSchemeSnapshot,
    IssueTypeScreenSchemeMappingSnapshot,
    IssueTypeScreenSchemeSnapshot,
    IssueTypeSnapshot,
    ProjectSnapshot,
    ScreenSchemeSnapshot,
    ScreenSnapshot,
    ScreenTabSnapshot,
)


# ── Pagination ────────────────────────────────────────────────────────
async def paginate(
    client: JiraHTTPClient,
    path: str,
    *,
    page_size: int = 50,
    extra_params: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield items from a Jira admin endpoint, page by page.

    Handles three response shapes seen on DC and Cloud admin endpoints:
      1. Bare JSON array (e.g. /field, /issuetype on older DC) - single page.
      2. ``{"values": [...], "isLast": bool, "startAt": int, ...}`` - paginated.
      3. ``{"values": [...], "nextPage": "..."}`` style - paginated, treat
         absence of more values as end.
    """
    start = 0
    while True:
        params = {"startAt": start, "maxResults": page_size}
        if extra_params:
            params.update(extra_params)
        resp = await client.get_json(path, params=params)

        if isinstance(resp, list):
            for item in resp:
                yield item
            return

        values = resp.get("values") or []
        for item in values:
            yield item

        if not values:
            return
        if resp.get("isLast") is True:
            return
        # Defensive: if a server omits isLast, advance startAt until empty page.
        next_start = resp.get("startAt", start) + len(values)
        if next_start == start:
            return
        start = next_start


# ── Custom fields ─────────────────────────────────────────────────────
def is_custom_field(payload: dict[str, Any]) -> bool:
    return str(payload.get("id", "")).startswith("customfield_")


def parse_custom_field(payload: dict[str, Any]) -> CustomFieldSnapshot:
    schema = payload.get("schema") or {}
    type_id = str(schema.get("custom", "") or schema.get("type", ""))
    return CustomFieldSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        type_id=type_id,
        options={},
    )


SELECT_TYPE_FRAGMENTS = ("select", "multiselect", "radiobuttons", "checkboxes")


def is_select_style(type_id: str) -> bool:
    """Heuristic for needing an option fetch. Matches DC and Cloud type IDs."""
    return any(frag in type_id.lower() for frag in SELECT_TYPE_FRAGMENTS)


def parse_field_options(payloads: list[dict[str, Any]]) -> dict[str, str]:
    """Parse a /field/{id}/option response into a name->id map."""
    result: dict[str, str] = {}
    for opt in payloads:
        name = str(opt.get("value", "") or opt.get("name", ""))
        opt_id = str(opt.get("id", ""))
        if name and opt_id:
            result[name] = opt_id
    return result


# ── Issue types ───────────────────────────────────────────────────────
def parse_issuetype(payload: dict[str, Any]) -> IssueTypeSnapshot:
    return IssueTypeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        subtask=bool(payload.get("subtask", False)),
    )


# ── Projects ──────────────────────────────────────────────────────────
def parse_project(payload: dict[str, Any]) -> ProjectSnapshot:
    lead = payload.get("lead") or {}
    lead_name = lead.get("name") or lead.get("accountId") or lead.get("displayName")
    return ProjectSnapshot(
        id=str(payload["id"]),
        key=str(payload.get("key", "")),
        name=str(payload.get("name", "")),
        lead=str(lead_name) if lead_name else None,
        project_type_key=str(payload.get("projectTypeKey", "")),
        style=str(payload.get("style", "classic")),
    )


# ── Screens ───────────────────────────────────────────────────────────
def parse_screen_header(payload: dict[str, Any]) -> ScreenSnapshot:
    """Build a ScreenSnapshot with no tabs. Tabs are populated by a follow-up call."""
    return ScreenSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        tabs=(),
    )


def parse_screen_tab(payload: dict[str, Any], fields: list[dict[str, Any]]) -> ScreenTabSnapshot:
    return ScreenTabSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        fields=tuple(str(f.get("id", "")) for f in fields if f.get("id")),
    )


# ── Screen schemes ────────────────────────────────────────────────────
def parse_screen_scheme(payload: dict[str, Any]) -> ScreenSchemeSnapshot:
    screens = payload.get("screens") or {}
    mappings: dict[str, str] = {}
    for op, ref in screens.items():
        if ref is None:
            continue
        # On Cloud the value is an int; on DC it can be {"id": "..."} or just an int/str.
        if isinstance(ref, dict):
            sid = ref.get("id")
        else:
            sid = ref
        if sid is not None:
            mappings[str(op)] = str(sid)
    return ScreenSchemeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        mappings=mappings,
    )


# ── Issue type screen schemes ─────────────────────────────────────────
def parse_itss_header(payload: dict[str, Any]) -> IssueTypeScreenSchemeSnapshot:
    """Issue-type screen scheme with empty mappings; mappings come from /mapping."""
    return IssueTypeScreenSchemeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        mappings=(),
    )


def parse_itss_mapping(payload: dict[str, Any]) -> IssueTypeScreenSchemeMappingSnapshot:
    return IssueTypeScreenSchemeMappingSnapshot(
        issuetype_id=str(payload.get("issueTypeId", "default")),
        screen_scheme_id=str(payload.get("screenSchemeId", "")),
    )


# ── Field configurations ──────────────────────────────────────────────
def parse_field_configuration_header(payload: dict[str, Any]) -> FieldConfigurationSnapshot:
    return FieldConfigurationSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        items={},
    )


def parse_field_configuration_item(payload: dict[str, Any]) -> FieldConfigurationItemSnapshot:
    return FieldConfigurationItemSnapshot(
        field_id=str(payload.get("id", "")),
        required=bool(payload.get("isRequired", False) or payload.get("required", False)),
        hidden=bool(payload.get("isHidden", False) or payload.get("hidden", False)),
        description=str(payload.get("description", "")),
    )


# ── Field configuration schemes ───────────────────────────────────────
def parse_fcs_header(payload: dict[str, Any]) -> FieldConfigurationSchemeSnapshot:
    return FieldConfigurationSchemeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        mappings=(),
    )


def parse_fcs_mapping(payload: dict[str, Any]) -> FieldConfigurationSchemeMappingSnapshot:
    return FieldConfigurationSchemeMappingSnapshot(
        issuetype_id=str(payload.get("issueTypeId", "default")),
        field_configuration_id=str(payload.get("fieldConfigurationId", "")),
    )


# ── Issue type schemes ────────────────────────────────────────────────
def parse_issuetype_scheme_header(payload: dict[str, Any]) -> IssueTypeSchemeSnapshot:
    """Scheme without members; members come from /issuetypescheme/mapping."""
    default_id = payload.get("defaultIssueTypeId")
    return IssueTypeSchemeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        issuetype_ids=(),
        default_issuetype_id=str(default_id) if default_id is not None else None,
    )


def parse_issuetype_scheme_mapping(payload: dict[str, Any]) -> tuple[str, str]:
    """One row of /issuetypescheme/mapping. Returns (scheme_id, issuetype_id)."""
    return (
        str(payload.get("issueTypeSchemeId", "")),
        str(payload.get("issueTypeId", "")),
    )
