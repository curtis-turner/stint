#!/usr/bin/env python3
"""Offline repro: TMP apply recovers orphaned project-scoped fields.

Covers the delete-side gap in ISSUE_DRAFT.md TMP-C that per-op persistence
alone does not close: a field that exists in Jira but is absent from both
``state.fields`` and the schema has no alias->id mapping, so the old
``plan_tmp`` could neither avoid duplicating it on retry nor ever target it
with ``--allow-delete``.

Post-fix behavior, exercised here without a live tenant:

1. A declared field whose name+type match an untracked reflected field is
   *adopted* (``AdoptField``) instead of re-created. State gains the
   alias->id mapping, so the field becomes deletable by the normal path.
2. A leftover field that matches nothing in the schema is reported by
   ``find_untracked_fields`` instead of staying invisible.
3. Once adopted, removing the alias from schema makes ``--allow-delete``
   emit a normal ``DeleteField``.

Usage:
    uv run python tools/tmp_orphan_field_recovery_repro.py
"""

from __future__ import annotations

import asyncio

from stint.client.auth import APITokenAuth
from stint.client.http import JiraHTTPClient
from stint.dialects.jira.tmp import TmpDialect
from stint.dialects.jira.tmp.desired import TmpDesired, TmpDesiredField
from stint.dialects.jira.tmp.models import TmpProjectContext, TmpSnapshot
from stint.dialects.jira.tmp.ops import TmpApplyContext
from stint.dialects.jira.tmp.reconcile import (
    AdoptField,
    DeleteField,
    apply_tmp_plan,
    find_untracked_fields,
    plan_tmp,
)
from stint.dialects.jira.tmp.state import TmpState
from stint.fields import SelectField, TextField
from stint.state.snapshot import CustomFieldSnapshot, ServerInfoSnapshot, Snapshot

BASE = "https://example.atlassian.net"


def _snapshot() -> TmpSnapshot:
    return TmpSnapshot(
        snapshot=Snapshot(
            server_info=ServerInfoSnapshot(deployment_type="Cloud", version="x", base_url=BASE),
            custom_fields={
                # An orphan from a pre-per-op-persist run. Same name+type as
                # the schema's "Severity" -> adoptable.
                "customfield_9": CustomFieldSnapshot(
                    id="customfield_9",
                    name="Severity",
                    type_id=SelectField.jira_type_id,
                    options={"S1": "1", "S2": "2"},
                ),
                # Abandoned leftover that matches nothing in the schema.
                "customfield_99": CustomFieldSnapshot(
                    id="customfield_99",
                    name="Leftover Field",
                    type_id=TextField.jira_type_id,
                    options={},
                ),
            },
        )
    )


async def run() -> int:
    desired = TmpDesired(
        project_key="VM",
        fields={
            "severity": TmpDesiredField(
                alias="severity",
                name="Severity",
                type_key=SelectField.jira_type_id,
                options=("S1", "S2"),
            )
        },
    )
    snapshot = _snapshot()
    state = TmpState()

    changes = plan_tmp(desired, snapshot, state)
    print("Planned changes:")
    for change in changes:
        print(f"  {type(change).__name__:<16} {change}")
    print()

    if not any(isinstance(c, AdoptField) and c.field_id == "customfield_9" for c in changes):
        print("FAIL: matching orphan was not adopted.")
        return 1

    untracked = find_untracked_fields(desired, snapshot, state)
    print("Reported untracked leftovers:")
    for field in untracked:
        print(f"  - {field.field_id}  {field.name!r}  ({field.type_id})")
    print()
    if [f.field_id for f in untracked] != ["customfield_99"]:
        print("FAIL: expected only customfield_99 to be reported as untracked.")
        return 1

    # Adoption is state-only: no field-create call happens. This is the
    # retry-idempotency guarantee -- there is no wire route mocked at all, so
    # an accidental create would fail loudly.
    client = JiraHTTPClient(BASE, auth=APITokenAuth(email="you@example.com", token="tok"))
    ctx = TmpApplyContext(
        dialect=TmpDialect(client),
        project=TmpProjectContext(cloud_id="c", project_id="10001", project_uuid="u", key="VM", name="VM"),
        state=state,
    )
    await apply_tmp_plan(ctx, changes, desired, snapshot)
    await client.close()
    if state.fields != {"severity": "customfield_9"}:
        print(f"FAIL: adoption did not record state: {state.fields!r}")
        return 1

    # Once adopted, dropping the alias from schema reaches the normal delete
    # path -- the exact thing that used to be impossible for an orphan.
    delete_plan = plan_tmp(TmpDesired(project_key="VM"), snapshot, state, allow_delete=True)
    print("Plan after removing the alias from schema (--allow-delete):")
    for change in delete_plan:
        print(f"  {type(change).__name__:<16} {change}")
    print()
    if not any(isinstance(c, DeleteField) and c.alias == "severity" for c in delete_plan):
        print("FAIL: adopted field is still unreachable by --allow-delete.")
        return 1

    print("PASS: orphan adopted without duplication, leftover reported, adopted field deletable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
