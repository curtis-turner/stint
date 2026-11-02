"""Helpers shared inside the Jira dialect layer.

Pagination, payload parsers for each admin object. ``JiraCloudDialect``
builds URLs against its own api_root and delegates parsing here.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator
from typing import Any

from stint.client.http import JiraHTTPClient
from stint.exceptions import ReflectionError
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
    SecurityLevelMemberSnapshot,
    SecurityLevelSnapshot,
    SecuritySchemeSnapshot,
)

# ── Pagination ────────────────────────────────────────────────────────
# Safety bounds for the page loop itself (a run-away pager must terminate,
# not hang). _MAX_PAGES: any tenant reflection stays well below this.
_MAX_PAGES = 1000
_ECHO_MISMATCH_REFETCHES = 2  # extra fetches before trusting a cursor bug


async def paginate(
    client: JiraHTTPClient,
    path: str,
    *,
    envelope_key: str = "values",
    page_size: int = 50,
    extra_params: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield items from a Jira admin endpoint, page by page.

    Handles four response shapes seen on Jira admin endpoints:
      1. Bare JSON array (e.g. /field, /issuetype on older Cloud) - single page.
      2. ``{"values": [...], "isLast": bool, "startAt": int, ...}`` - paginated
         by ``startAt``.
      3. ``{"values": [...], "nextPage": "https://..."}`` - cursor-paginated:
         the server-provided next-page URL is followed verbatim, so this
         works for endpoints that ignore ``startAt``. A ``nextPage`` URL is
         never followed more than once (a self-referential loop raises).
      4. A non-paginated dict envelope carrying no ``startAt`` and no
         ``isLast`` (e.g. ``{"issueSecuritySchemes": [...]}``) - yielded items
         of the single page only. Trusting such a response to paginate makes
         the endpoint ignore our ``startAt`` and serve page 1 forever.

    Raise ``ReflectionError`` on:
      - a dict response with no ``"{envelope_key}"`` key (unrecognized
        envelope).
      - a repeated ``nextPage`` URL, verified across refetches (a
        self-referential cursor; single stale repeats recover).
      - more than ``_MAX_PAGES`` pages (a run-away pager must never spin the
        generator forever).

    Echo-mismatch policy: a response whose ``startAt`` echo disagrees with
    the requested page is first re-fetched once (transient glitch guard), then
    — if it persists — treated as the server's authoritative cursor: warn
    once, align to it, and keep going. Jira's experimental v2 Field Schemes
    endpoints have been observed doing this in the wild; the previous
    hard-abort made such tenants unreflectable, while silently trusting our
    own numbering risks skipping items. A misaligned cursor never loops: a
    later echo *behind* our aligned position is treated as clamped
    duplicates already seen (end of pages).
    """
    start = 0
    pages_fetched = 0
    seen_next_urls: set[str] = set()
    warned_cursor = False
    next_url: str | None = None
    preloaded: dict[str, Any] | None = None  # recovered-from-glitch response
    while True:
        if preloaded is not None:
            resp = preloaded
            preloaded = None
        elif next_url is not None:
            resp = await client.get_json(next_url)
            next_url = None
        else:
            params = {"startAt": start, "maxResults": page_size}
            if extra_params:
                params.update(extra_params)
            resp = await client.get_json(path, params=params)

        if isinstance(resp, list):
            for item in resp:
                yield item
            return

        if not isinstance(resp, dict) or envelope_key not in resp:
            raise ReflectionError(
                f"{path} returned an unrecognized pagination envelope: "
                f"expected a list or a dict with a '{envelope_key}' key, got "
                f"{type(resp).__name__}: {str(resp)[:200]!r}"
            )

        pages_fetched += 1
        if pages_fetched > _MAX_PAGES:
            raise ReflectionError(
                f"{path} exceeded {_MAX_PAGES} pages without an end marker "
                f"(isLast/empty values); refusing to loop forever"
            )

        echoed = resp.get("startAt")
        if echoed is not None and echoed != start:
            try:
                echoed_int = int(echoed)
            except (TypeError, ValueError):
                echoed_int = start  # unparsable echo: treat as aligned
            if echoed_int < start:
                # Clamped echo: the server clamped our out-of-range startAt
                # back onto a page it already served — those items were
                # yielded on an earlier iteration. Treat the clamp as
                # end-of-pages instead of re-serving duplicates or spinning.
                return
            # The server's cursor sits AHEAD of ours — a page the client did
            # not ask for. Verify before believing: an inconsistent echo can
            # be a transient glitch (a stale envelope racing our request),
            # so refetch the same page a couple of times first. Only a
            # persistent mismatch is treated as the server's authoritative
            # numbering (seen in the wild on Atlassian's experimental v2
            # Field Schemes endpoints): warn once, align, continue.
            for _ in range(_ECHO_MISMATCH_REFETCHES):
                params = {"startAt": start, "maxResults": page_size}
                if extra_params:
                    params.update(extra_params)
                resp = await client.get_json(path, params=params)
                if not isinstance(resp, dict) or envelope_key not in resp:
                    break  # shape handling below takes it from here
                new_echoed = resp.get("startAt")
                try:
                    new_int = int(new_echoed) if new_echoed is not None else None
                except (TypeError, ValueError):
                    break
                if new_int is None or new_int == start:
                    echoed_int = start  # transient glitch: back in sync
                    break
                echoed_int = new_int
            if echoed_int > start:
                if not warned_cursor:
                    warnings.warn(
                        f"{path} echoes startAt={echoed!r} while we requested "
                        f"startAt={start!r}; this endpoint does not honor page "
                        f"numbering — continuing aligned to the server's echo",
                        stacklevel=2,
                    )
                    warned_cursor = True
                start = echoed_int

        values = resp.get(envelope_key) or []
        for item in values:
            yield item

        cursor = resp.get("nextPage")
        if isinstance(cursor, str) and cursor.startswith("http"):
            if cursor in seen_next_urls:
                # Recover before raising: a stale envelope race can serve a
                # previous page's cursor wrapped around current rows (seen on
                # the v2 Field Schemes endpoints: page-1 rows in an envelope
                # whose echo claimed startAt=50 and whose nextPage pointed at
                # startAt=50 — itself). If following the cursor a few more
                # times moves off it, the repeat was transient; only a
                # persistent repeat is a real self-referential cursor.
                recovered: dict[str, Any] | None = None
                for _ in range(_ECHO_MISMATCH_REFETCHES):
                    cand = await client.get_json(cursor)
                    if isinstance(cand, dict) and cand.get("nextPage") != cursor:
                        recovered = cand
                        break
                if recovered is None:
                    raise ReflectionError(
                        f"{path} returned the same nextPage URL two or more "
                        f"times (verified by {_ECHO_MISMATCH_REFETCHES} "
                        f"refetches); refusing to loop forever on a "
                        f"self-referential cursor: {cursor!r}"
                    )
                # Align numbering to the recovered page so its own ``startAt``
                # echo doesn't re-trigger the mismatch machinery (which would
                # refetch the unrelated base path).
                try:
                    start = int(recovered.get("startAt", start))
                except (TypeError, ValueError):
                    pass
                preloaded = recovered
                continue
            seen_next_urls.add(cursor)
            next_url = cursor
            continue

        if not values:
            return
        if resp.get("isLast") is True:
            return
        # Bare-envelope defense: a true PageBean always echoes ``startAt``
        # (every Atlassian paginated admin endpoint does). A response with
        # neither ``startAt`` nor ``isLast`` is a non-paginated envelope that
        # will ignore our ``startAt=N`` and serve the same page forever —
        # treat it as a single page instead of looping (this is exactly the
        # mistake /issuesecurityschemes made when routed through paginate).
        if "startAt" not in resp:
            return
        start += len(values)


# ── Custom fields ─────────────────────────────────────────────────────
def is_custom_field(payload: dict[str, Any]) -> bool:
    return str(payload.get("id", "")).startswith("customfield_")


def parse_custom_field(payload: dict[str, Any]) -> CustomFieldSnapshot:
    schema = payload.get("schema") or {}
    type_id = str(schema.get("custom", "") or schema.get("type", ""))
    # /field/search includes description; diffing needs it (L21).
    return CustomFieldSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        type_id=type_id,
        options={},
        description=str(payload.get("description", "") or ""),
        # Present on GET /field and GET /field/search. None/absent means the
        # field's search template is unset, so JQL clauses on it fail.
        searcher_key=str(payload.get("searcherKey", "") or ""),
    )


SELECT_TYPE_FRAGMENTS = ("select", "multiselect", "radiobuttons", "checkboxes")


def is_select_style(type_id: str) -> bool:
    """Heuristic for needing an option fetch. Matches Jira Cloud type IDs."""
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
    scope = payload.get("scope") or {}
    return IssueTypeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        subtask=bool(payload.get("subtask", False)),
        project_scoped=scope.get("type") == "PROJECT",
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
        # /project/search returns the description; diffing needs it (L21).
        description=str(payload.get("description", "") or ""),
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
        # Cloud screens are referenced by id; a few payload shapes wrap it as {"id": "..."}.
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
        description=str(payload.get("description") or ""),
        items={},
    )


def parse_field_configuration_item(payload: dict[str, Any]) -> FieldConfigurationItemSnapshot:
    return FieldConfigurationItemSnapshot(
        field_id=str(payload.get("id", "")),
        required=bool(payload.get("isRequired", False) or payload.get("required", False)),
        hidden=bool(payload.get("isHidden", False) or payload.get("hidden", False)),
        description=str(payload.get("description", "")),
    )


def parse_field_scheme_field(payload: dict[str, Any]) -> FieldConfigurationItemSnapshot:
    """Parse one row of ``GET /config/fieldschemes/{id}/fields`` (v2).

    v2 has no ``isHidden``: a field that is not associated with the scheme
    is absent from this listing, which is the hiding mechanism. Required can
    be set at the scheme default (``parameters``) or per work type
    (``workTypeParameters``); the snapshot carries one bool, so any required
    override counts as required.
    """
    parameters = payload.get("parameters") or {}
    work_type_parameters = payload.get("workTypeParameters") or []
    required = bool(parameters.get("isRequired")) or any(
        bool((wt or {}).get("isRequired")) for wt in work_type_parameters
    )
    return FieldConfigurationItemSnapshot(
        field_id=str(payload.get("fieldId", "")),
        required=required,
        hidden=False,
        description=str(parameters.get("description") or ""),
    )


# ── Field configuration schemes ───────────────────────────────────────
def parse_fcs_header(payload: dict[str, Any]) -> FieldConfigurationSchemeSnapshot:
    return FieldConfigurationSchemeSnapshot(
        id=str(payload["id"]),
        name=str(payload.get("name", "")),
        description=str(payload.get("description") or ""),
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


def parse_issue_security_scheme(payload: dict[str, Any]) -> SecuritySchemeSnapshot:
    """Parse a single issue security scheme entry from the /issuesecurityschemes endpoint.

    The paginated list returns entries like:
    {
      "id": 10000,
      "name": "Default Issue Security Scheme",
      "description": "Description for the default issue security scheme",
      "defaultSecurityLevelId": 10021,
      "self": "https://your-domain.atlassian.net/rest/api/3/issuesecurityschemes/10000"
    }
    """
    scheme_id = payload.get("id")
    if scheme_id is None:
        raise ReflectionError(f"issue security scheme payload has no 'id': {str(payload)[:200]!r}")
    levels: list[SecurityLevelSnapshot] = []
    # If the entry includes levels (some API versions do), parse them
    for level_entry in payload.get("levels", []):
        if not isinstance(level_entry, dict):
            raise ReflectionError(f"issue security scheme payload has a non-object level: {level_entry!r}")
        members: list[SecurityLevelMemberSnapshot] = []
        for m in level_entry.get("members", []):
            if not isinstance(m, dict):
                raise ReflectionError(f"security level {level_entry.get('id')!r} has a non-object member: {m!r}")
            holder = m.get("holder", {})
            members.append(
                SecurityLevelMemberSnapshot(
                    id=str(m.get("id", "")),
                    type=str(holder.get("type", "unknown")),
                    name=str(holder.get("name", "")),
                    # holder.parameter is the group name / accountId; fall back
                    # to name for payloads that only carry the display name.
                    parameter=str(holder.get("parameter", "") or holder.get("name", "")),
                )
            )
        levels.append(
            SecurityLevelSnapshot(
                id=str(level_entry.get("id", "")),
                name=str(level_entry.get("name", "")),
                description=str(level_entry.get("description", "")),
                is_default=level_entry.get("isDefault", False),
                members=tuple(members),
            ),
        )

    return SecuritySchemeSnapshot(
        id=str(scheme_id),
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        default_security_level_id=str(payload.get("defaultSecurityLevelId", ""))
        if payload.get("defaultSecurityLevelId")
        else None,
        levels=levels,
    )
