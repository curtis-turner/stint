#!/usr/bin/env python3
"""Offline repro: TMP apply attaches declared fields to a brand-new work type.

Drives the full ``stint apply`` apply_tmp_plan path end-to-end through
respx, without a live tenant. Demonstrates the fix for ISSUE_DRAFT.md
TMP-A (a freshly-created work type's layout has zero custom items, and
the pre-fix code wrote that emptiness back, leaving the work type's edit
screen empty even though the schema declared fields).

Prints the wire body of the final ``write_layout`` PUT so you can see
exactly which fields the apply run attaches. Pre-fix this body had zero
``items``; post-fix it carries one synthesized ``TmpLayoutItem`` per
declared custom field.

Usage:
    uv run python tools/tmp_apply_layout_repro.py

Note: the schema is defined at module level (not inside a helper
function) because Pydantic's resolution of ``Annotated`` metadata on a
``CustomField`` reference doesn't survive the function-local namespace
when ``from __future__ import annotations`` is in effect. The sibling
test files avoid this by also defining schemas at module level.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal

import httpx
import respx

from stint import CustomField, IssueType, Project
from stint.client.auth import APITokenAuth
from stint.client.http import JiraHTTPClient
from stint.dialects.jira.tmp import TmpDialect
from stint.dialects.jira.tmp.desired import build_tmp_desired
from stint.dialects.jira.tmp.graphql import FIELDS_GATEWAY_PATH, LAYOUT_READ_PATH
from stint.dialects.jira.tmp.internal_rest import ISSUE_LAYOUTS_PATH, SIMPLIFIED_PATH
from stint.dialects.jira.tmp.models import TmpProjectContext, TmpSnapshot
from stint.dialects.jira.tmp.ops import TmpApplyContext
from stint.dialects.jira.tmp.reconcile import apply_tmp_plan, plan_tmp
from stint.dialects.jira.tmp.state import TmpState
from stint.fields import SelectField, TextField
from stint.state.snapshot import ServerInfoSnapshot, Snapshot

BASE = "https://example.atlassian.net"


# Module-level schema (see note in module docstring).
severity_cf = CustomField(alias="severity", name="Severity", type=SelectField, options=["S1", "S2"])
root_cause_cf = CustomField(alias="root_cause", name="Root Cause", type=TextField)


class Bug(IssueType):
    __alias__ = "bug"
    __title__ = "Bug"
    __description__ = "A bug"
    severity: Annotated[Literal["S1", "S2"], severity_cf]
    root_cause: Annotated[str, root_cause_cf]


class Vm(Project):
    __key__ = "VM"
    __style__ = "team-managed"
    __issuetypes__ = [Bug]


async def run() -> int:
    desired = build_tmp_desired(Vm)
    snapshot = TmpSnapshot(
        snapshot=Snapshot(server_info=ServerInfoSnapshot(deployment_type="Cloud", version="x", base_url=BASE))
    )
    state = TmpState()
    changes = plan_tmp(desired, snapshot, state)

    print("Planned changes:")
    for change in changes:
        print(f"  {type(change).__name__:<16} alias={change.alias!r}")
    print()

    with respx.mock:
        # Field create: returns the two declared fields with predictable ids.
        def _field_dispatch(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            query = body["query"]
            if "Severity" in query:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "jira": {
                                "createCustomFieldInProjectAndAddToAllIssueTypes": {
                                    "success": True,
                                    "fieldAssociationWithIssueTypes": {
                                        "field": {
                                            "fieldId": "customfield_1",
                                            "name": "Severity",
                                            "description": "",
                                            "scope": "PROJECT",
                                        },
                                        "fieldOptions": {"edges": []},
                                    },
                                }
                            }
                        }
                    },
                )
            if "Root Cause" in query:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "jira": {
                                "createCustomFieldInProjectAndAddToAllIssueTypes": {
                                    "success": True,
                                    "fieldAssociationWithIssueTypes": {
                                        "field": {
                                            "fieldId": "customfield_2",
                                            "name": "Root Cause",
                                            "description": "",
                                            "scope": "PROJECT",
                                        },
                                        "fieldOptions": {"edges": []},
                                    },
                                }
                            }
                        }
                    },
                )
            raise AssertionError(f"unexpected field-create query: {query[:200]!r}")

        respx.post(f"{BASE}{FIELDS_GATEWAY_PATH}").mock(side_effect=_field_dispatch)
        # Work-type create: returns a fresh id for the new Bug work type.
        respx.post(f"{BASE}{SIMPLIFIED_PATH}/project/10001/settings/issuetype").mock(
            return_value=httpx.Response(
                201, json={"id": "10007", "name": "Bug", "avatarId": 10321, "hierarchyLevel": 0}
            )
        )
        # Layout read for the brand-new work type: ZERO custom items (the bug).
        respx.post(f"{BASE}{LAYOUT_READ_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issueLayoutConfiguration": {
                            "__typename": "JiraIssueLayoutConfigurationResult",
                            "issueLayoutResult": {
                                "id": "layout-x",
                                "name": "VM-Bug",
                                "usageInfo": {
                                    "edges": [
                                        {
                                            "currentProject": True,
                                            "node": {
                                                "layoutOwners": [
                                                    {
                                                        "__typename": "JiraIssueLayoutIssueTypeOwner",
                                                        "id": "10007",
                                                        "name": "Bug",
                                                        "description": "A bug",
                                                        "avatarId": "10321",
                                                        "iconUrl": "x",
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                },
                                "containers": [],  # <-- the bug: no items, no positions
                            },
                            "metadata": {"configuration": {"items": {"nodes": []}}},
                        }
                    }
                },
            )
        )
        # Capture the write_layout PUT so we can print what the apply sent.
        put_route = respx.put(f"{BASE}{ISSUE_LAYOUTS_PATH}/layout-x").mock(
            return_value=httpx.Response(
                200,
                json={
                    "owners": [
                        {
                            "type": "ISSUE_TYPE",
                            "data": {
                                "id": 10007,
                                "name": "Bug",
                                "description": "A bug",
                                "avatarId": 10321,
                                "iconUrl": "x",
                            },
                        }
                    ],
                    "issueLayoutConfig": {"items": []},
                },
            )
        )

        client = JiraHTTPClient(BASE, auth=APITokenAuth(email="you@example.com", token="tok"))
        dialect = TmpDialect(client)
        project_ctx = TmpProjectContext(cloud_id="c", project_id="10001", project_uuid="u", key="VM", name="VM")
        ctx = TmpApplyContext(dialect=dialect, project=project_ctx, state=state)
        await apply_tmp_plan(ctx, changes, desired, snapshot)
        await client.close()

    body = json.loads(put_route.calls.last.request.content)
    sent = body["issueLayoutConfig"]["items"]
    print("write_layout PUT body (issueLayoutConfig.items):")
    print(json.dumps(sent, indent=2))
    print()
    if len(sent) == 0:
        print("FAIL: zero items written -- TMP-A bug is back.")
        return 1
    keys = sorted(item["key"] for item in sent)
    expected = ["customfield_1", "customfield_2"]
    if keys != expected:
        print(f"FAIL: expected items for {expected}, got {keys}.")
        return 1
    print(f"PASS: write_layout attached {len(sent)} field items to the brand-new work type.")
    print(f"      field ids on the layout: {keys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
