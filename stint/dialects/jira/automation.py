"""Automation rule-management API client (Jira Cloud).

The rule-management REST API lives on a different host from the tenant's
Jira instance: ``https://api.atlassian.com/automation/public/jira/{cloudId}/
rest/v1/``. Auth is the same API token (HTTP basic) stint already uses;
Forge and OAuth-2.0 apps cannot call these endpoints, so the API-token path
is the supported one. From 8 December 2026 the request paths must match the
documented paths exactly or the API answers 403 — the paths below are the
documented ones, verbatim.

GA 29 August 2025; delete added 3 March 2026. Mixed into
``JiraCloudDialect`` so ops keep calling ``dialect.<method>``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from stint.client.http import JiraHTTPClient
from stint.dialects.jira import common
from stint.exceptions import PermissionError, ReflectionError

# Page-loop budget shared with the other reflect loops.
_MAX_AUTOMATION_PAGES = common._MAX_PAGES


class AutomationRuleClient:
    """Mixin: the rule-management endpoints, one method per documented operation."""

    AUTOMATION_API_BASE: ClassVar[str] = "https://api.atlassian.com/automation/public/jira"

    _cloud_id_cache: str | None = None

    # The host dialect's HTTP client (declared so the mixin type-checks;
    # JiraCloudDialect supplies it).
    client: JiraHTTPClient

    # ── Tenant / permission plumbing ──────────────────────────────────
    async def get_cloud_id(self, *, force: bool = False) -> str:
        """The tenant's cloudId (``GET /_edge/tenant_info``), cached.

        Every automation URL needs it. Same host as the tenant (path-only
        call), same client.
        """
        if self._cloud_id_cache is not None and not force:
            return self._cloud_id_cache
        payload = await self.client.get_json("/_edge/tenant_info")
        cloud_id = payload.get("cloudId") if isinstance(payload, dict) else None
        if not cloud_id:
            raise ReflectionError(f"/_edge/tenant_info returned no cloudId: {str(payload)[:200]!r}")
        self._cloud_id_cache = str(cloud_id)
        return self._cloud_id_cache

    def _automation_url(self, cloud_id: str, path: str) -> str:
        """Absolute rule-management URL for a documented path fragment."""
        return f"{self.AUTOMATION_API_BASE}/{cloud_id}/rest/v1/{path}"

    async def probe_rule_management(self) -> None:
        """Fail before any write when the account cannot manage rules.

        The docs do not name the required level (community reports say Jira
        admin); a 403 from the cheapest read is the only reliable probe.
        Cached on the dialect instance — one extra GET per apply at most.
        """
        cloud_id = await self.get_cloud_id()
        try:
            await self.client.get_json(self._automation_url(cloud_id, "rule/summary"), params={"limit": 1})
        except PermissionError as e:
            raise PermissionError(
                f"the authenticated account cannot read automation rules (403 from the "
                f"rule-management API): managing automation rules requires a Jira "
                f"administrator-level account. stint stops before writing anything. "
                f"({e})"
            ) from e

    # ── Reads ─────────────────────────────────────────────────────────
    async def list_rule_summaries(self) -> list[dict[str, Any]]:
        """All rule summaries (``GET rule/summary``, cursor pagination).

        Each summary carries uuid/name/state/scope ARIs but not the rule
        body; fetch payloads with ``get_rule``.
        """
        cloud_id = await self.get_cloud_id()
        summaries: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_AUTOMATION_PAGES):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.client.get_json(self._automation_url(cloud_id, "rule/summary"), params=params)
            if not isinstance(page, dict) or "data" not in page:
                raise ReflectionError(f"GET rule/summary returned an unrecognized envelope: {str(page)[:200]!r}")
            summaries.extend(entry for entry in page.get("data") or [] if isinstance(entry, dict))
            cursor = _next_cursor(page.get("links"))
            if not cursor:
                return summaries
        raise ReflectionError(f"GET rule/summary exceeded {_MAX_AUTOMATION_PAGES} pages")

    async def get_rule(self, rule_uuid: str) -> dict[str, Any]:
        """One rule payload (``GET rule/{uuid}``) — the ``rule`` body."""
        cloud_id = await self.get_cloud_id()
        payload = await self.client.get_json(self._automation_url(cloud_id, f"rule/{rule_uuid}"))
        if isinstance(payload, dict) and isinstance(payload.get("rule"), dict):
            return payload["rule"]
        raise ReflectionError(f"GET rule/{rule_uuid} returned {type(payload).__name__} instead of a rule envelope")

    async def find_rule_uuid_by_name(self, name: str) -> str | None:
        """Match an existing rule by exact (case-insensitive) name — the
        adoption fallback for rules declared after they were built by hand."""
        for summary in await self.list_rule_summaries():
            if str(summary.get("name", "")).lower() == name.lower():
                uuid = summary.get("uuid")
                if uuid:
                    return str(uuid)
        return None

    # ── Writes ────────────────────────────────────────────────────────
    async def create_rule(self, rule: dict[str, Any]) -> str:
        """Create a rule (``POST rule``) → its uuid.

        The body has the same shape as the ``GET rule/{uuid}`` response, so
        an exported payload round-trips. ``ruleScopeARIs`` may be empty at
        create; the scope op pins it right after.
        """
        cloud_id = await self.get_cloud_id()
        result = await self.client.post_json(self._automation_url(cloud_id, "rule"), json={"rule": rule})
        uuid = result.get("ruleUuid") if isinstance(result, dict) else None
        if not uuid:
            raise ReflectionError(f"POST rule returned no ruleUuid: {str(result)[:200]!r}")
        return str(uuid)

    async def update_rule(self, rule_uuid: str, rule: dict[str, Any]) -> None:
        """Replace a rule's payload (``PUT rule/{uuid}``).

        Component ids are only required for pre-existing components; new
        ones are created and removed ones deleted as needed.
        """
        cloud_id = await self.get_cloud_id()
        await self.client.put_json(self._automation_url(cloud_id, f"rule/{rule_uuid}"), json={"rule": rule})

    async def set_rule_state(self, rule_uuid: str, *, enabled: bool) -> None:
        """Enable or disable a rule (``PUT rule/{uuid}/state``)."""
        cloud_id = await self.get_cloud_id()
        await self.client.put_json(
            self._automation_url(cloud_id, f"rule/{rule_uuid}/state"),
            json={"value": "ENABLED" if enabled else "DISABLED"},
        )

    async def set_rule_scope(self, rule_uuid: str, rule_scope_apis: list[str]) -> None:
        """Set a rule's project scope (``PUT rule/{uuid}/rule-scope``).

        ``rule_scope_apis`` are project ARIs. A rule scoped to several
        projects is one rule with a multi-project scope, not copies.
        """
        cloud_id = await self.get_cloud_id()
        await self.client.put_json(
            self._automation_url(cloud_id, f"rule/{rule_uuid}/rule-scope"),
            json={"ruleScopeARIs": list(rule_scope_apis)},
        )

    async def delete_rule(self, rule_uuid: str) -> None:
        """Delete a disabled rule (``DELETE rule/{uuid}``).

        Jira refuses enabled rules — the op disables first. Deleting an
        already-deleted rule 404s; ops treat that as done.
        """
        cloud_id = await self.get_cloud_id()
        await self.client.delete(self._automation_url(cloud_id, f"rule/{rule_uuid}"))


def _next_cursor(links: Any) -> str | None:
    """The cursor out of a summary page's ``links.next`` query string.

    The links field carries query parameters (``?cursor=...&limit=...``)
    since the August 2025 shape change; parse the cursor out, None when
    there is no next page.
    """
    if not isinstance(links, dict):
        return None
    next_link = links.get("next")
    if not next_link:
        return None
    text = str(next_link)
    marker = "cursor="
    idx = text.find(marker)
    if idx < 0:
        return None
    rest = text[idx + len(marker) :]
    cursor = rest.split("&", 1)[0]
    return cursor or None
