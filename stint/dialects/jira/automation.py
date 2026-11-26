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

import asyncio
import secrets
import time
import warnings
from typing import Any, ClassVar

from stint.client.http import JiraHTTPClient
from stint.dialects.jira import common
from stint.exceptions import NotFoundError, PermissionError, ReflectionError, StintError, TransportError
from stint.state.snapshot import AutomationRuleSnapshot

# Page-loop budget shared with the other reflect loops.
_MAX_AUTOMATION_PAGES = common._MAX_PAGES


class AutomationRuleClient:
    """Mixin: the rule-management endpoints, one method per documented operation."""

    AUTOMATION_API_BASE: ClassVar[str] = "https://api.atlassian.com/automation/public/jira"

    _cloud_id_cache: str | None = None
    _automation_actor_cache: str | None = None

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
        an exported payload round-trips. ``ruleScopeARIs`` must be present
        (the scope op pins multi-project scopes right after).

        Measured-live quirk: the endpoint *intermittently* answers 400
        "Failed to create rule" for identical, valid payloads and then
        accepts a retry (see notes/automation_api_research.md). Unlike a
        shape error — which answers "could not be parsed" or a field-scoped
        message — this marker is retried with backoff.
        """
        cloud_id = await self.get_cloud_id()
        path = self._automation_url(cloud_id, "rule")
        attempt = 0
        while True:
            try:
                result = await self.client.post_json(path, json={"rule": rule})
                uuid = result.get("ruleUuid") if isinstance(result, dict) else None
                if not uuid:
                    raise ReflectionError(f"POST rule returned no ruleUuid: {str(result)[:200]!r}")
                return str(uuid)
            except StintError as e:
                transient = "Failed to create rule" in str(e)
                if not transient or attempt >= 2:
                    raise
                attempt += 1
                await asyncio.sleep(2.0 * 2 ** (attempt - 1))

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

        Jira refuses enabled rules — the op disables first. Two live
        quirks: the endpoint answers **415 without a Content-Type header**
        even though the request has no body, and deleting an
        already-deleted rule 404s (ops treat that as done).
        """
        cloud_id = await self.get_cloud_id()
        await self.client.delete(
            self._automation_url(cloud_id, f"rule/{rule_uuid}"),
            headers={"Content-Type": "application/json"},
        )

    # ── Actor resolution ──────────────────────────────────────────────
    async def reflect_automation_rules(self) -> dict[str, AutomationRuleSnapshot]:
        """Reflect all automation rules (``rule/summary`` + ``GET rule/{uuid}``).

        Rule-management lives on api.atlassian.com, outside the tenant REST
        root; a tenant that never touches automation (or an account the
        rule API refuses) must not break the whole reflect, so failures
        degrade to an empty map. The request's reflect contract is summary
        then per-rule GET — payloads for every summarized rule, which keeps
        the diff exact at the cost of one GET per rule (fine at automation
        rule counts tenants actually have; revisit if that changes).
        """
        try:
            summaries = await self.list_rule_summaries()
        except (PermissionError, NotFoundError, TransportError) as e:
            warnings.warn(f"automation rule reflection skipped ({e})", stacklevel=2)
            return {}
        rules: dict[str, AutomationRuleSnapshot] = {}
        for summary in summaries:
            uuid = str(summary.get("uuid", ""))
            if not uuid:
                continue
            payload: dict[str, Any] = {}
            try:
                payload = await self.get_rule(uuid)
            except (NotFoundError, TransportError):
                pass  # summary without a readable body: keep the summary shape
            rules[uuid] = AutomationRuleSnapshot(
                uuid=uuid,
                name=str(summary.get("name", payload.get("name", ""))),
                state=str(summary.get("state", payload.get("state", "")) or ""),
                scope_apis=tuple(str(a) for a in summary.get("ruleScopeARIs") or []),
                payload=payload,
            )
        return rules

    async def find_automation_actor_id(self) -> str:
        """The "Automation for Jira" app account's accountId, cached.

        Create requires an ``actor`` that exists, is active, and has
        product access — the app account is the default (and the safest:
        it cannot be deactivated by offboarding). Found by
        ``accountType == "app"`` + display name, never by the ``557058:``
        id prefix (the prefix misses most app accounts and matches real
        humans). Raises when the tenant has no such account.
        """
        if self._automation_actor_cache is not None:
            return self._automation_actor_cache
        start = 0
        page_size = 200
        for _ in range(_MAX_AUTOMATION_PAGES):
            payload = await self.client.get_json(
                "/rest/api/3/users/search",
                params={"startAt": start, "maxResults": page_size},
            )
            users = payload if isinstance(payload, list) else []
            for user in users:
                if not isinstance(user, dict):
                    continue
                if (
                    user.get("accountType") == "app"
                    and str(user.get("displayName", "")).strip() == "Automation for Jira"
                ):
                    if user.get("accountId"):
                        self._automation_actor_cache = str(user["accountId"])
                        return self._automation_actor_cache
            if len(users) < page_size:
                break
            start += page_size
        raise ReflectionError(
            "no 'Automation for Jira' app account found on this tenant — every "
            "Automation rule needs an actor; declare one with actor=User('...') "
            "on the AutomationRule, or check that Automation is enabled on the site"
        )


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


def mint_rule_uuid() -> str:
    """A fresh RFC 9562 UUIDv7, minted client-side for every create.

    Create rejects an omitted uuid and a non-V7 uuid with the generic
    "could not be parsed" 400 (measured live; see
    notes/automation_api_research.md). ``uuid.uuid7`` exists from Python
    3.14; older interpreters get the hand-rolled form (unix-ts-ms |
    version 7 | rand_a | variant | rand_b).
    """
    import uuid

    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())
    ms = time.time_ns() // 1_000_000
    value = (ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62  # RFC variant bits
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def complete_rule_payload(
    payload: dict[str, Any],
    *,
    uuid: str,
    name: str,
    description: str,
    state: str,
    author_account_id: str,
    actor_account_id: str,
    scope_apis: list[str],
) -> dict[str, Any]:
    """Fill the create/update payload with the fields the API requires.

    The rule-management API answers its generic "could not be parsed" 400
    when any of ``actor``, ``authorAccountId``, ``components``,
    ``ruleScopeARIs``, ``canOtherRuleTrigger``, ``notifyOnError``,
    ``name``, ``state``, ``trigger``, ``writeAccessType``, or
    ``collaborators`` is missing (measured live: omitting any of these
    reads as a missing field, not a value error — the collaborator list is
    documented-optional but this build refuses its absence).
    ``description``/``labels`` are optional. The declaration wins over the
    payload on name/description/state, and stint always owns uuid (minted
    V7 on create, the recorded one on update), author, actor, and scope.
    """
    body = dict(payload)
    body["uuid"] = uuid
    body["name"] = name
    body["description"] = description
    body["state"] = state
    body["authorAccountId"] = author_account_id
    # The REST payload's actor carries the id under "actor" (the export
    # file's "value" key is a different shape — sending it reads as a
    # missing actor and create fails with "Failed to create rule").
    body["actor"] = {"type": "ACCOUNT_ID", "actor": actor_account_id}
    body["ruleScopeARIs"] = scope_apis
    body.setdefault("collaborators", [])
    body.setdefault("canOtherRuleTrigger", False)
    body.setdefault("notifyOnError", "FIRSTERROR")
    body.setdefault("labels", [])
    body.setdefault("writeAccessType", "UNRESTRICTED")
    if not body.get("components"):
        raise ReflectionError(
            f"rule {name!r} has no components — the API requires at least one valid "
            f'condition or action ("Automation rule must contain at least one valid '
            f'condition or action")'
        )
    return body
