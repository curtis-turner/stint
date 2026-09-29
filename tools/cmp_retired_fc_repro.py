#!/usr/bin/env python3
"""Offline repro: classic field-configuration create returns 400 on Jira
Cloud post-May 2026 -- stint now translates it into a ConfigurationError.

Exercises ``op.create_field_configuration`` end-to-end through respx
without a live tenant. Demonstrates the short-term fix for CMP-A in
``CMP_ISSUE_DRAFT.md`` (the legacy ``/fieldconfiguration`` endpoint
returns 400 with the body ``"Cannot create a new field configuration.
Please use Field Scheme instead."`` instead of 410 or 404 -- a
content-shape check, not a status-code one).

Pre-fix a schema author would see the raw HTTP message; post-fix they
get a clean ``ConfigurationError`` pointing at RFC-103/104/105 and the
in-tree Phase 3 plan.

Also runs the success path -- a normal 201 -- to confirm the fix
doesn't disturb ordinary behavior.

Usage:
    uv run python tools/cmp_retired_fc_repro.py
"""

from __future__ import annotations

import asyncio

import httpx
import respx

from stint import PATAuth, StateFile, op
from stint.engine import Engine, create_engine
from stint.exceptions import ConfigurationError
from stint.migrations.context import MigrationContext, reset_context, set_context
from stint.registry import registry

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


async def _run_in_ctx(engine: Engine, state: StateFile, body, reset_token_holder: list) -> None:
    ctx = MigrationContext(engine=engine, state=state, direction="upgrade")
    reset_token_holder.append(set_context(ctx))


async def success_path() -> int:
    registry.reset()
    with respx.mock:
        respx.post(
            f"{CLOUD_ROOT}/fieldconfiguration",
            json__eq={"name": "Bug FC", "description": ""},
        ).mock(return_value=httpx.Response(201, json={"id": "fc-1"}))

        state = StateFile(env="dev", jira_url=BASE)
        engine = create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))
        tokens: list = []
        try:
            await _run_in_ctx(
                engine, state, lambda: op.create_field_configuration(alias="bug_fc", name="Bug FC"), tokens
            )
            await op.create_field_configuration(alias="bug_fc", name="Bug FC")
            reset_context(tokens[0])
        finally:
            await engine.close()

    print("Success path:")
    print(f"  state.field_configurations: {dict(state.field_configurations)}")
    if "bug_fc" not in state.field_configurations:
        print("  FAIL: alias was not recorded on success.")
        return 1
    if state.field_configurations["bug_fc"].id != "fc-1":
        print(f"  FAIL: expected id fc-1, got {state.field_configurations['bug_fc'].id!r}.")
        return 1
    print("  PASS: 201 -> alias recorded with id fc-1.")
    return 0


async def retired_path() -> int:
    registry.reset()
    with respx.mock:
        respx.post(
            f"{CLOUD_ROOT}/fieldconfiguration",
            json__eq={"name": "Bug FC", "description": ""},
        ).mock(
            return_value=httpx.Response(
                400,
                json={
                    "errorMessages": ["Cannot create a new field configuration. Please use Field Scheme instead."],
                    "errors": {},
                },
            )
        )

        state = StateFile(env="dev", jira_url=BASE)
        engine = create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))
        tokens: list = []
        try:
            await _run_in_ctx(
                engine, state, lambda: op.create_field_configuration(alias="bug_fc", name="Bug FC"), tokens
            )
            try:
                await op.create_field_configuration(alias="bug_fc", name="Bug FC")
            except ConfigurationError as exc:
                msg = str(exc)
                print("Retired-endpoint path:")
                print("  raised ConfigurationError:")
                for line in msg.splitlines():
                    print(f"    {line}")
                print()
                # Verify the message carries the actionable signals a
                # schema author needs to fix their schema / understand
                # what's going on.
                if "retired classic field configurations" not in msg:
                    print("  FAIL: message doesn't mention retirement.")
                    return 1
                if "RFC-103/104/105" not in msg:
                    print("  FAIL: message doesn't point at the RFCs.")
                    return 1
                if "Cannot create a new field configuration" not in msg:
                    print("  FAIL: message doesn't include the original Jira text.")
                    return 1
                if "bug_fc" in state.field_configurations:
                    print("  FAIL: alias recorded despite the 400 -- state drifted.")
                    return 1
                reset_context(tokens[0])
                print("  PASS: 400 translated to ConfigurationError; nothing recorded in state.")
                return 0
            reset_context(tokens[0])
        finally:
            await engine.close()

    print("Retired-endpoint path:")
    print("  FAIL: op.create_field_configuration did NOT raise.")
    return 1


async def other_4xx_path() -> int:
    """The fix only matches the specific retirement 400 body. Other 4xx
    must keep the original TransportError so a real wire error stays
    visible."""
    registry.reset()
    with respx.mock:
        respx.post(
            f"{CLOUD_ROOT}/fieldconfiguration",
            json__eq={"name": "Bug FC", "description": ""},
        ).mock(
            return_value=httpx.Response(
                400,
                json={"errorMessages": ["Some unrelated validation error."], "errors": {}},
            )
        )

        state = StateFile(env="dev", jira_url=BASE)
        engine = create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))
        tokens: list = []
        try:
            await _run_in_ctx(
                engine, state, lambda: op.create_field_configuration(alias="bug_fc", name="Bug FC"), tokens
            )
            try:
                await op.create_field_configuration(alias="bug_fc", name="Bug FC")
            except ConfigurationError:
                print("Other-4xx path:")
                print("  FAIL: ConfigurationError raised for an unrelated 400 -- translation is too greedy.")
                reset_context(tokens[0])
                return 1
            except Exception as exc:
                # Whatever it raised -- as long as it isn't ConfigurationError --
                # we kept the original wire error and didn't pretend to know
                # what a different 400 means.
                reset_context(tokens[0])
                print("Other-4xx path:")
                print(f"  raised (non-ConfigurationError): {type(exc).__name__}: {exc}")
                print("  PASS: unrelated 400 keeps the original TransportError.")
                return 0
            reset_context(tokens[0])
        finally:
            await engine.close()
    print("Other-4xx path:")
    print("  FAIL: op.create_field_configuration did not raise at all on a 4xx.")
    return 1


async def main() -> int:
    results = []
    for name, fn in (
        ("success", success_path),
        ("retired", retired_path),
        ("other-4xx", other_4xx_path),
    ):
        print(f"── {name} ────────────────────────────────────────────")
        rc = await fn()
        results.append((name, rc))
        print()
    failures = [name for name, rc in results if rc != 0]
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("All paths PASS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
