#!/usr/bin/env python3
"""Pin the screen-lock behavior of a Jira Cloud tenant (JRACLOUD-98054).

The restricted-Edit-screen pattern locks fields in the UI only. Since the
override-screen-security deprecation, fields off the Edit screen are still
editable through the REST API on company-managed projects, and Rovo exposes
that to every licensed user. The permission scheme (EDIT_ISSUES) is the real
gate; the screen is behavioral.

This script verifies that on YOUR tenant with YOUR identities, so the threat
model in docs/guides/locking.md is a measured fact, not an assumption. Run it
against a throwaway issue on a test project.

What it does, per identity:

1. ``GET /rest/api/3/issue/{key}/editmeta`` — is the field offered for edit?
2. With ``--apply``: ``PUT /rest/api/3/issue/{key}`` with the field set.

Expected on current Jira Cloud: the field is absent from editmeta for BOTH
identities, and (with --apply) BOTH writes succeed — including the restricted
identity, if it holds EDIT_ISSUES. If the restricted identity's write fails,
your tenant still enforces the screen; record that instead of assuming.

Usage:

    export SERVICE_EMAIL=svc-automation@example.com
    export SERVICE_TOKEN=...
    export USER_EMAIL=alice@example.com
    export USER_TOKEN=...

    python scripts/verify_screen_lock.py \\
        --url https://you.atlassian.net \\
        --issue SEC-1 \\
        --field customfield_10042 \\
        --value-json '{"value": "S1"}' \\
        --apply

``--value-json`` is the raw Jira field value payload: for a select field use
``{"value": "..."}`` or ``{"id": "..."}``; for a text field the value is the
bare JSON string, e.g. ``'"probe"'``. Without ``--apply`` the script only
reads editmeta and changes nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def _basic_auth(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _request(url: str, *, method: str, auth: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _probe(label: str, base: str, key: str, field_id: str, email: str, token: str, apply_value: str | None) -> None:
    auth = _basic_auth(email, token)
    root = f"{base.rstrip('/')}/rest/api/3"
    status, body = _request(f"{root}/issue/{key}/editmeta", method="GET", auth=auth)
    offered = field_id in body if status == 200 else None
    print(f"[{label}] GET editmeta -> {status}; field {field_id} offered: {offered}")

    if apply_value is None:
        return
    status, body = _request(
        f"{root}/issue/{key}",
        method="PUT",
        auth=auth,
        body={"fields": {field_id: json.loads(apply_value)}},
    )
    verdict = "SUCCEEDED (screen lock not enforced for this identity)" if status == 204 else f"REJECTED ({status})"
    print(f"[{label}] PUT {field_id} -> {verdict}")
    if status != 204:
        print(f"        body: {body[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="Jira Cloud base URL")
    parser.add_argument("--issue", required=True, help="Issue key of a throwaway test issue")
    parser.add_argument("--field", required=True, help="Field id, e.g. customfield_10042")
    parser.add_argument("--value-json", help="Raw JSON field value for --apply")
    parser.add_argument("--apply", action="store_true", help="Actually write the field (destructive on the test issue)")
    parser.add_argument("--service-email", default=os.environ.get("SERVICE_EMAIL", ""))
    parser.add_argument("--service-token", default=os.environ.get("SERVICE_TOKEN", ""))
    parser.add_argument("--user-email", default=os.environ.get("USER_EMAIL", ""))
    parser.add_argument("--user-token", default=os.environ.get("USER_TOKEN", ""))
    args = parser.parse_args()

    if args.apply and not args.value_json:
        parser.error("--apply requires --value-json")

    rc = 0
    _probe(
        "service",
        args.url,
        args.issue,
        args.field,
        args.service_email,
        args.service_token,
        args.value_json if args.apply else None,
    )
    _probe(
        "user",
        args.url,
        args.issue,
        args.field,
        args.user_email,
        args.user_token,
        args.value_json if args.apply else None,
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
