#!/usr/bin/env python3
"""Live-tenant smoke gate for 0.21.0 automation rule schemes (.stint env).

Drives stint's own rule-management client (``JiraCloudDialect`` /
``AutomationRuleClient``) against the real tenant, so the URLs, bodies,
and auth are exactly what ops will send at apply time.

Phases (each prints a VERDICT; every created rule is deleted on exit):

0. **read** — ``GET /_edge/tenant_info`` (cloudId), the rule-management
   probe (``GET rule/summary?limit=1``), and a full summary listing.
   Read-only.
1. **round-trip** — create a clearly-named rule DISABLED, read it back,
   flip state ENABLED -> DISABLED, delete it. Inert on the tenant (no
   scope, no actions).
2. **guard-shape** — build the real ``ControlFieldGuard`` payload with
   stint, substitute the live field id + a resolved writer, create it
   DISABLED, read back what Jira echoed (component types, condition
   value), run the diff normalizer declared-vs-reflected (the idempotence
   claim against a REAL echo), PUT the same payload, then delete.
3. **live-revert** (``--live-revert``, destructive on ONE issue) — create
   the guard with an allow-list that matches nobody, scope it to the
   project, ENABLE it, edit the field as the authenticated account (an
   out-of-band write by construction), and poll until the value is
   restored, printing the guard's changelog entry and comment. Then
   disable + delete the rule and restore the original value if the guard
   did not. The request's live acceptance test for one field type; run
   again per additional field type.

Credentials: parses ``.stint/verify.env`` (JIRA_URL / SERVICE_EMAIL /
SERVICE_TOKEN) or takes --url/--email/--token. Never prints secrets.

Usage:

    uv run python scripts/live_gate_021.py --phases 0,1,2
    uv run python scripts/live_gate_021.py --live-revert --issue SEC-31 --field customfield_10320
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stint.client.auth import APITokenAuth
from stint.client.http import JiraHTTPClient
from stint.dialects.jira.automation import complete_rule_payload, mint_rule_uuid
from stint.dialects.jira.cloud import JiraCloudDialect
from stint.exceptions import NotFoundError, PermissionError, StintError
from stint.fields import CustomField, SelectField
from stint.schema.automation import (
    ControlFieldGuard,
    User,
    normalized_rule_payload,
    ref_placeholder,
    substitute_refs,
)

SMOKE_RULE_NAME = "stint smoke 0.21: round-trip (safe to delete)"
SMOKE_GUARD_NAME = "stint smoke 0.21: guard shape (safe to delete)"
VERIFY_ENV = Path(".stint/verify.env")


def _load_verify_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if VERIFY_ENV.exists():
        for line in VERIFY_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    env.update(
        {k: v for k, v in os.environ.items() if k.startswith(("SERVICE_", "JIRA_URL", "ISSUE_KEY", "FIELD_ID")) and v}
    )
    return env


class _SmokeDialect(JiraCloudDialect):
    """The real Cloud dialect — the automation client plus resolve_user,
    my_account_id, and the actor lookup the complete payload needs."""


class Smoke:
    def __init__(self, *, url: str, email: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.email = email
        self.token = token
        self.client = JiraHTTPClient(self.url, APITokenAuth(email, token))
        self.dialect: JiraCloudDialect = _SmokeDialect(self.client)
        self.created_rules: list[str] = []

    async def close(self) -> None:
        await self.client.close()

    def track(self, uuid: str) -> str:
        self.created_rules.append(uuid)
        return uuid

    async def cleanup(self) -> None:
        """Disable + delete every rule this run created (best-effort)."""
        for uuid in list(self.created_rules):
            try:
                await self.dialect.set_rule_state(uuid, enabled=False)
            except StintError:
                pass
            try:
                await self.dialect.delete_rule(uuid)
                print(f"  cleanup: deleted rule {uuid}")
            except StintError as e:
                print(f"  cleanup FAILED for rule {uuid} (left DISABLED): {e}")
            self.created_rules.remove(uuid)

    # ── raw v3 REST with status codes (the client maps errors to StintError) ──
    async def jira_get(self, path: str) -> tuple[int, Any]:
        try:
            return 200, await self.client.get_json(f"/rest/api/3{path}")
        except NotFoundError:
            return 404, {}
        except StintError as e:
            return getattr(e, "status_code", 0) or 500, str(e)

    async def jira_put(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        try:
            await self.client.put_json(f"/rest/api/3{path}", json=body)
            return 204, {}
        except NotFoundError:
            return 404, {}
        except StintError as e:
            return getattr(e, "status_code", 0) or 500, str(e)

    async def resolve_account_id(self, value: str) -> str:
        if "@" not in value:
            return value
        return await self.dialect.resolve_user(value)


async def phase_read(smoke: Smoke) -> bool:
    """Phase 0: connectivity, permission probe, rule listing."""
    print("=== phase 0: read (no writes) ===")
    try:
        cloud_id = await smoke.dialect.get_cloud_id()
        print(f"tenant_info cloudId: {cloud_id}")
    except StintError as e:
        print(f"FAIL: /_edge/tenant_info: {e}")
        return False

    try:
        await smoke.dialect.probe_rule_management()
        print("rule-management probe: 200 (the account can manage rules)")
    except PermissionError as e:
        print(f"FAIL: probe 403 — the authenticated account cannot manage rules:\n  {e}")
        return False

    summaries = await smoke.dialect.list_rule_summaries()
    print(f"rules on tenant: {len(summaries)}")
    for s in summaries[:20]:
        scopes = s.get("ruleScopeARIs") or []
        print(f"  {s.get('uuid')} {s.get('name')!r} state={s.get('state')} scope={len(scopes)} ARI(s)")
    if len(summaries) > 20:
        print(f"  ... and {len(summaries) - 20} more")
    print("VERDICT phase 0: PASS")
    return True


def _round_trip_rule() -> dict[str, Any]:
    return {
        "name": SMOKE_RULE_NAME,
        "description": "Created by scripts/live_gate_021.py; deleted before exit.",
        "trigger": {
            "component": "TRIGGER",
            "parentId": None,
            "conditionParentId": None,
            "schemaVersion": 2,
            "type": "jira.issue.field.changed",
            "value": {"changeType": "ANY_CHANGE", "fields": [{"value": "summary", "type": "field"}], "actions": []},
            "children": [],
            "conditions": [],
            "connectionId": None,
        },
        "components": [
            {
                "component": "ACTION",
                "parentId": None,
                "conditionParentId": None,
                "schemaVersion": 2,
                "type": "jira.issue.comment",
                "value": {
                    "comment": "smoke rule should never run (no scope)",
                    "publicComment": False,
                    "commentVisibility": None,
                    "sendNotifications": False,
                    "addCommentOnce": False,
                },
                "children": [],
                "conditions": [],
                "connectionId": None,
            }
        ],
        "state": "DISABLED",
        "canOtherRuleTrigger": False,
        "notifyOnError": "FIRSTERROR",
        "labels": [],
        "writeAccessType": "UNRESTRICTED",
    }


async def phase_roundtrip(smoke: Smoke, *, project_id: str) -> bool:
    """Phase 1: create -> read -> state flip -> delete, of an inert rule."""
    print("=== phase 1: rule round-trip (created DISABLED, project-scoped, no other actions) ===")
    cloud_id = await smoke.dialect.get_cloud_id()
    author = await smoke.dialect.my_account_id()
    actor = await smoke.dialect.find_automation_actor_id()
    print(f"author (authenticated): {author}; actor (Automation for Jira app): {actor}")
    rule = complete_rule_payload(
        _round_trip_rule(),
        uuid=mint_rule_uuid(),
        name=SMOKE_RULE_NAME,
        description="Created by scripts/live_gate_021.py; deleted before exit.",
        state="DISABLED",
        author_account_id=author,
        actor_account_id=actor,
        scope_apis=[f"ari:cloud:jira:{cloud_id}:project/{project_id}"],
    )
    try:
        uuid = smoke.track(await smoke.dialect.create_rule(rule))
        print(f"POST rule -> 201 uuid={uuid}")
    except StintError as e:
        print(f"FAIL: POST rule rejected the payload shape: {e}")
        return False

    ok = True
    try:
        echoed = await smoke.dialect.get_rule(uuid)
        print(f"GET rule/{uuid} -> name={echoed.get('name')!r} state={echoed.get('state')}")
        print(f"  trigger type echoed: {echoed.get('trigger', {}).get('type')!r}")
        print(f"  components echoed: {[c.get('type') for c in echoed.get('components') or []]}")
        if echoed.get("name") != SMOKE_RULE_NAME:
            ok = False
            print("FAIL: name did not round-trip")
        diff = _first_diff(
            normalized_rule_payload(rule, is_guard=False),
            normalized_rule_payload(echoed, is_guard=False),
        )
        if diff:
            print(f"NOTE: normalizer sees declared-vs-reflected diff at {diff}:")
            print(f"  declared:  {json.dumps(_pick(rule, diff))[:300]}")
            print(f"  reflected: {json.dumps(_pick(echoed, diff))[:300]}")
        else:
            print("  normalizer: declared == reflected (idempotence holds on a REAL echo)")
    except StintError as e:
        ok = False
        print(f"FAIL: GET rule/{uuid}: {e}")

    try:
        await smoke.dialect.set_rule_state(uuid, enabled=True)
        await smoke.dialect.set_rule_state(uuid, enabled=False)
        print("PUT state ENABLED then DISABLED -> OK")
    except StintError as e:
        ok = False
        print(f"FAIL: state flip: {e}")

    try:
        await smoke.dialect.delete_rule(uuid)
        smoke.created_rules.remove(uuid)
        print("DELETE rule -> OK (delete-on-disabled works)")
    except StintError as e:
        ok = False
        print(f"FAIL: delete: {e}")

    print(f"VERDICT phase 1: {'PASS' if ok else 'FAIL'}")
    return ok


def _guard_payload_sync(field_id: str, writer_account_id: str, allow_value: str) -> dict[str, Any]:
    """The real ControlFieldGuard payload with live ids substituted in."""
    severity = CustomField(alias="bug_severity", name="Severity", type=SelectField, options=["Low", "High"])
    guard = ControlFieldGuard(
        alias="smoke_guard",
        name=SMOKE_GUARD_NAME,
        description="Created by scripts/live_gate_021.py; deleted before exit.",
        fields=[severity],
        allowed_writers=[User(allow_value)],
        on_violation=["revert", "comment"],
    )
    payload = guard.build_payload([severity])
    resolved = {
        ref_placeholder("field", "bug_severity"): field_id,
        ref_placeholder("user", "writer0"): writer_account_id,
    }
    return substitute_refs(payload, resolved)


async def _guard_payload(smoke: Smoke, *, field_id: str, allow: str) -> dict[str, Any]:
    writer_id = await smoke.resolve_account_id(allow)
    return _guard_payload_sync(field_id, writer_id, allow)


async def phase_guard_shape(smoke: Smoke, *, project_id: str, field_id: str, allow: str) -> bool:
    """Phase 2: the generated guard payload, created DISABLED, verified against the echo."""
    print("=== phase 2: guard payload shape (created DISABLED, project-scoped) ===")
    payload = await _guard_payload(smoke, field_id=field_id, allow=allow)
    cloud_id = await smoke.dialect.get_cloud_id()
    payload = complete_rule_payload(
        payload,
        uuid=mint_rule_uuid(),
        name=SMOKE_GUARD_NAME,
        description="Created by scripts/live_gate_021.py; deleted before exit.",
        state="DISABLED",
        author_account_id=await smoke.dialect.my_account_id(),
        actor_account_id=await smoke.dialect.find_automation_actor_id(),
        scope_apis=[f"ari:cloud:jira:{cloud_id}:project/{project_id}"],
    )
    try:
        uuid = smoke.track(await smoke.dialect.create_rule(payload))
        print(f"POST rule (guard shape) -> 201 uuid={uuid}")
    except StintError as e:
        print(f"FAIL: Jira rejected the guard payload shape: {e}")
        return False

    ok = True
    try:
        echoed = await smoke.dialect.get_rule(uuid)
        print(f"trigger echoed: {echoed.get('trigger', {}).get('type')!r}")
        print(f"trigger value echoed: {json.dumps(echoed.get('trigger', {}).get('value'))[:400]}")
        print(f"component types echoed: {[c.get('type') for c in echoed.get('components') or []]}")
        for c in echoed.get("components") or []:
            if c.get("type") == "jira.user.condition":
                print(f"user condition value echoed: {json.dumps(c.get('value'))[:400]}")
            if c.get("type") == "jira.issue.edit":
                print(f"edit advancedFields echoed: {c.get('value', {}).get('advancedFields')}")
        diff = _first_diff(
            normalized_rule_payload(payload, is_guard=True),
            normalized_rule_payload(echoed, is_guard=True),
        )
        if diff:
            ok = False
            print(f"FAIL: normalizer sees a diff at {diff}:")
            print(f"  declared:  {json.dumps(_pick(payload, diff))[:400]}")
            print(f"  reflected: {json.dumps(_pick(echoed, diff))[:400]}")
        else:
            print("  normalizer: declared == reflected (the guard diff is idempotent against a REAL echo)")

        await smoke.dialect.update_rule(uuid, payload)
        print("PUT rule (same payload) -> OK (update shape accepted)")
        echoed2 = await smoke.dialect.get_rule(uuid)
        diff2 = _first_diff(
            normalized_rule_payload(payload, is_guard=True),
            normalized_rule_payload(echoed2, is_guard=True),
        )
        print(f"  post-update normalizer diff: {diff2 or 'none'}")
    except StintError as e:
        ok = False
        print(f"FAIL: {e}")

    print(f"VERDICT phase 2: {'PASS' if ok else 'FAIL'}")
    return ok


async def phase_live_revert(
    smoke: Smoke,
    *,
    project_id: str,
    field_id: str,
    issue_key: str,
    timeout: float,
) -> bool:
    """Phase 3: the live revert acceptance on one issue, one field type."""
    print(f"=== phase 3: live revert on {issue_key} ({field_id}); allow-list matches nobody ===")

    status, editmeta = await smoke.jira_get(f"/issue/{issue_key}/editmeta")
    if status != 200 or not isinstance(editmeta, dict) or field_id not in editmeta.get("fields", {}):
        print(f"FAIL: cannot read editmeta for {issue_key} ({status})")
        return False
    allowed = [v.get("value") for v in (editmeta["fields"][field_id].get("allowedValues") or []) if v.get("value")]
    status, issue = await smoke.jira_get(f"/issue/{issue_key}?fields={field_id}")
    if status != 200 or not isinstance(issue, dict):
        print(f"FAIL: cannot read {issue_key} ({status})")
        return False
    field_state = issue.get("fields", {}).get(field_id) or {}
    current = field_state.get("value")
    original_payload = dict(field_state)
    print(f"current value: {current!r}; allowed: {allowed}")
    target = next((v for v in allowed if v != current), None)
    if target is None:
        print("FAIL: no alternative option to write")
        return False

    payload = await _guard_payload(smoke, field_id=field_id, allow="00000000-0000-0000-0000-000000000000")
    cloud_id = await smoke.dialect.get_cloud_id()
    payload = complete_rule_payload(
        payload,
        uuid=mint_rule_uuid(),
        name=SMOKE_GUARD_NAME,
        description="Created by scripts/live_gate_021.py; deleted before exit.",
        state="ENABLED",
        author_account_id=await smoke.dialect.my_account_id(),
        actor_account_id=await smoke.dialect.find_automation_actor_id(),
        scope_apis=[f"ari:cloud:jira:{cloud_id}:project/{project_id}"],
    )
    try:
        uuid = smoke.track(await smoke.dialect.create_rule(payload))
        print(f"guard created ENABLED, scoped to project {project_id}: uuid={uuid}")
    except StintError as e:
        print(f"FAIL: guard create: {e}")
        return False

    ok = True
    uuid_for_cleanup = uuid
    restored_at: float | None = None
    try:
        t0 = time.monotonic()
        put_status, put_body = await smoke.jira_put(f"/issue/{issue_key}", {"fields": {field_id: {"value": target}}})
        print(f"violation PUT {field_id}={target!r} -> {put_status}")
        if put_status != 204:
            print(f"FAIL: violation write rejected: {str(put_body)[:300]}")
            return False

        value = None
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(3.0)
            status, now = await smoke.jira_get(f"/issue/{issue_key}?fields={field_id}")
            value = ((now.get("fields", {}) or {}).get(field_id) or {}).get("value") if isinstance(now, dict) else None
            if value == current:
                restored_at = time.monotonic() - t0
                print(f"value restored to {current!r} after ~{restored_at:.0f}s")
                break
        if restored_at is None:
            ok = False
            print(f"FAIL: value not restored within {timeout:.0f}s (last value: {value!r})")

        status, log = await smoke.jira_get(f"/issue/{issue_key}/changelog")
        entries = (log.get("values") or []) if isinstance(log, dict) else []
        for e in entries:
            age = time.time() - _epoch(e.get("created", ""))
            if age > 900:
                continue
            author = (e.get("author") or {}).get("displayName", "?")
            items = [(i.get("field"), i.get("fromString"), i.get("toString")) for i in e.get("items") or []]
            print(f"  changelog ({age:.0f}s ago) by {author}: {items}")
        status, comments = await smoke.jira_get(f"/issue/{issue_key}/comment")
        body = json.dumps(comments) if isinstance(comments, dict) else ""
        print(f"guard comment present: {'Reverted an edit' in body}")
    finally:
        if restored_at is None:
            await smoke.jira_put(f"/issue/{issue_key}", {"fields": {field_id: original_payload}})
            print(f"  cleanup: restored {field_id} to its original value")
        try:
            await smoke.dialect.set_rule_state(uuid_for_cleanup, enabled=False)
            await smoke.dialect.delete_rule(uuid_for_cleanup)
            smoke.created_rules.remove(uuid_for_cleanup)
            print("  cleanup: guard disabled + deleted")
        except StintError as e:
            print(f"  cleanup FAILED (rule {uuid_for_cleanup} may remain, DISABLED): {e}")

    print(f"VERDICT phase 3: {'PASS' if ok else 'FAIL'}")
    return ok


def _first_diff(a: Any, b: Any, path: str = "") -> str | None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            d = _first_diff(a.get(key), b.get(key), f"{path}.{key}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path} (length {len(a)} != {len(b)})"
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            d = _first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else path


def _pick(obj: Any, path: str) -> Any:
    for part in path.replace("]", "").replace("[", ".").split("."):
        if not part:
            continue
        if part.isdigit():
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.get(part)
    return obj


def _epoch(jira_ts: str) -> float:
    try:
        return datetime.fromisoformat(jira_ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _project_id_from_state(path: Path) -> str | None:
    if not path.exists():
        return None
    import yaml

    state = yaml.safe_load(path.read_text())
    projects = (state.get("mappings") or {}).get("projects") or {}
    for mapping in projects.values():
        if mapping.get("id"):
            return str(mapping["id"])
    return None


async def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phases", default="0,1,2", help="Comma-separated phases: 0,1,2")
    parser.add_argument("--live-revert", action="store_true", help="Run phase 3 (destructive on ONE issue)")
    parser.add_argument("--issue", help="Scratch issue key (default: ISSUE_KEY from .stint/verify.env)")
    parser.add_argument("--field", help="Field id (default: FIELD_ID from .stint/verify.env)")
    parser.add_argument("--project-id", help="Numeric project id (default: from the devel state file)")
    parser.add_argument("--timeout", type=float, default=90.0, help="Seconds to wait for the revert")
    parser.add_argument("--url")
    parser.add_argument("--email")
    parser.add_argument("--token")
    args = parser.parse_args()

    env = _load_verify_env()
    url = args.url or env.get("JIRA_URL", "")
    email = args.email or env.get("SERVICE_EMAIL", "")
    token = args.token or env.get("SERVICE_TOKEN", "")
    if not (url and email and token):
        print("credentials missing: need JIRA_URL / SERVICE_EMAIL / SERVICE_TOKEN (.stint/verify.env or flags)")
        return 2

    phases = {int(p) for p in args.phases.split(",") if p.strip()}
    if args.live_revert:
        phases.add(3)
    args.issue = args.issue or env.get("ISSUE_KEY")
    args.field = args.field or env.get("FIELD_ID")
    if 3 in phases and not (args.issue and args.field):
        print("phase 3 needs --issue and --field (or ISSUE_KEY / FIELD_ID in .stint/verify.env)")
        return 2

    project_id = args.project_id or _project_id_from_state(Path("examples/company_managed/state/devel.yaml")) or ""
    if not project_id:
        print("project id unknown: pass --project-id (needed for the guard scope)")
        return 2

    smoke = Smoke(url=url, email=email, token=token)
    results: dict[int, bool] = {}
    try:
        if 0 in phases:
            results[0] = await phase_read(smoke)
        if 1 in phases:
            results[1] = await phase_roundtrip(smoke, project_id=project_id)
        if 2 in phases:
            results[2] = await phase_guard_shape(
                smoke, project_id=project_id, field_id=args.field or "customfield_10320", allow=email
            )
        if 3 in phases:
            results[3] = await phase_live_revert(
                smoke,
                project_id=project_id,
                field_id=args.field or "",
                issue_key=args.issue or "",
                timeout=args.timeout,
            )
    finally:
        await smoke.cleanup()
        await smoke.close()

    print("=== summary ===")
    for phase in sorted(results):
        print(f"phase {phase}: {'PASS' if results[phase] else 'FAIL'}")
    return 0 if results and all(results.values()) else 1


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
