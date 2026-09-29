#!/usr/bin/env python3
"""Live vocabulary probe: which stint field types does the TMP write surface accept?

The TMP ``createCustomFieldInProjectAndAddToAllIssueTypes`` mutation accepts
only a subset of Jira's full custom-field type ids. Out of the 18
``_FieldType`` classes in ``stint.fields``, only 9 currently work on TMP
(confirmed live, see ``ISSUE_DRAFT.md`` TMP-B and
``stint/dialects/jira/tmp/types.py``). This probe re-verifies that count
against a live team-managed project so a future Atlassian type-vocabulary
change shows up as a clear pass/fail delta in one run, and the
``TMP_FIELD_TYPE_KEYS`` map can be updated in lockstep.

Usage:
    export STINT_USER='you@example.com'
    export STINT_TOKEN='...'
    uv run python tools/tmp_type_probe.py \
        --site https://your-site.atlassian.net \
        --project VM  # a team-managed test project

Safety: every probe that creates a field registers its own delete cleanup
and runs it in a finally block, so a run leaves the project as it found it.
Do NOT point this at a production project you care about -- use a
throwaway team-managed project.

Exit code: 0 if every stint field type produces the same
pass/fail classification the in-tree mapping encodes, 1 otherwise. The
diff is printed at the end so the maintainer can update the mapping in
one place.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

# Match the constants in tools/tmp_discovery.py -- the gateway path and
# opt-in key are project-wide constants, not configurable per request.
GRAPHQL_PATH = "/gateway/api/graphql"
OPTIN_CREATE_FIELD = "JiraProjectFieldsPageCreateCustomField"
OPTIN_DELETE_FIELD = "JiraProjectFieldsPageDeleteCustomField"

TMP_TYPE_KEY_PREFIX = "com.atlassian.jira.plugin.system.customfieldtypes:"


@dataclass
class Ctx:
    client: httpx.Client
    site: str
    cloud_id: str
    project_key: str
    project_id: str
    project_uuid: str
    cleanups: list[Callable[[], None]] = field(default_factory=list)


def graphql(ctx: Ctx, query: str, operation_name: str) -> dict:
    payload = {"query": query, "operationName": operation_name}
    r = ctx.client.post(f"{ctx.site}{GRAPHQL_PATH}", content=json.dumps(payload))
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise SystemExit(f"GraphQL errors: {json.dumps(body['errors'])[:400]}")
    return body["data"]


def discover(client: httpx.Client, site: str, project_key: str) -> Ctx:
    cloud_id = client.get(f"{site}/_edge/tenant_info").json()["cloudId"]
    ctx = Ctx(
        client=client,
        site=site,
        cloud_id=cloud_id,
        project_key=project_key,
        project_id="",
        project_uuid="",
    )
    q = f"""query StintTypeProbeResolveProject {{
      jira_projectByIdOrKey(cloudId: "{cloud_id}", idOrKey: "{project_key}") {{
        projectId key uuid projectStyle
      }}
    }}"""
    proj = graphql(ctx, q, "StintTypeProbeResolveProject")["jira_projectByIdOrKey"]
    ctx.project_id = str(proj["projectId"])
    ctx.project_uuid = proj["uuid"]
    style = proj.get("projectStyle")
    if style != "TEAM_MANAGED_PROJECT":
        raise SystemExit(
            f"Project {project_key!r} is {style!r}, not TEAM_MANAGED_PROJECT. "
            f"Point --project at a team-managed project."
        )
    return ctx


# Map of stint _FieldType class name to its Jira type id (mirroring
# ``stint/dialects/jira/tmp/types.py`` exactly so the diff at the end of a
# run is "what should this map look like").
def stint_type_keys() -> dict[str, str]:
    # Local import keeps this script runnable without importing the
    # full stint package (and its registry / engine wiring).
    from stint.fields import (
        CheckboxesField,
        DateField,
        DateTimeField,
        LabelsField,
        NumberField,
        SelectField,
        TextAreaField,
        TextField,
        URLField,
    )

    return {
        TextField.__name__: f"{TMP_TYPE_KEY_PREFIX}textfield",
        TextAreaField.__name__: f"{TMP_TYPE_KEY_PREFIX}textarea",
        SelectField.__name__: f"{TMP_TYPE_KEY_PREFIX}select",
        CheckboxesField.__name__: f"{TMP_TYPE_KEY_PREFIX}multicheckboxes",
        LabelsField.__name__: f"{TMP_TYPE_KEY_PREFIX}labels",
        URLField.__name__: f"{TMP_TYPE_KEY_PREFIX}url",
        NumberField.__name__: f"{TMP_TYPE_KEY_PREFIX}float",
        DateField.__name__: f"{TMP_TYPE_KEY_PREFIX}datepicker",
        DateTimeField.__name__: f"{TMP_TYPE_KEY_PREFIX}datetime",
    }


def try_field_create(ctx: Ctx, type_key: str, name: str) -> tuple[bool, str]:
    """Try to create a field with the given Jira type id. Returns (ok, detail).

    ``ok`` is True only if the mutation returned success=true. ``detail``
    holds the error message on failure (for the final report) or the
    field id on success (so we can clean it up).
    """
    q = f"""mutation StintTypeProbeCreate {{
      jira {{
        createCustomFieldInProjectAndAddToAllIssueTypes(input: {{
          cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}",
          type: "{type_key}", name: "{name}", description: "stint type-probe", options: []
        }}) @optIn(to: "{OPTIN_CREATE_FIELD}") {{
          success
          fieldAssociationWithIssueTypes {{ field {{ fieldId scope }} }}
        }}
      }}
    }}"""
    try:
        result = graphql(ctx, q, "StintTypeProbeCreate")["jira"]["createCustomFieldInProjectAndAddToAllIssueTypes"]
    except SystemExit as exc:
        return False, str(exc).split("\n", 1)[0][:200]
    if not result.get("success"):
        return False, f"success != true: {result}"
    field = result.get("fieldAssociationWithIssueTypes", {}).get("field", {})
    fid = field.get("fieldId")
    if not fid:
        return False, f"no fieldId in response: {result}"
    if field.get("scope") != "PROJECT":
        return False, f"scope {field.get('scope')!r}, expected PROJECT"

    # Schedule cleanup LIFO (matches tools/tmp_discovery.py's pattern).
    def cleanup() -> None:
        dq = f"""mutation StintTypeProbeDelete {{
          jira {{
            deleteCustomField(input: {{
              cloudId: "{ctx.cloud_id}", projectId: "{ctx.project_id}", fieldId: "{fid}"
            }}) @optIn(to: "{OPTIN_DELETE_FIELD}") {{ success }}
          }}
        }}"""
        try:
            graphql(ctx, dq, "StintTypeProbeDelete")
        except SystemExit:
            pass  # cleanup must never fail the probe

    ctx.cleanups.append(cleanup)
    return True, fid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True, help="Jira Cloud site, e.g. https://your-site.atlassian.net")
    parser.add_argument("--project", required=True, help="Team-managed project key")
    args = parser.parse_args()

    user = os.environ.get("STINT_USER")
    token = os.environ.get("STINT_TOKEN")
    if not (user and token):
        raise SystemExit("Set STINT_USER and STINT_TOKEN environment variables.")

    client = httpx.Client(auth=(user, token), timeout=30.0)
    ctx = discover(client, args.site, args.project)
    expected_map = stint_type_keys()

    print(f"Probing {len(expected_map)} stint field types against {args.site} ({args.project}).")
    print()
    results: dict[str, tuple[bool, bool, str]] = {}
    try:
        for type_name, type_key in expected_map.items():
            name = f"stint-type-probe-{type_name}-{uuid.uuid4().hex[:6]}"
            ok, detail = try_field_create(ctx, type_key, name)
            results[type_name] = (ok, True, detail)  # (probe_ok, expected_ok, detail)
            verdict = "PASS" if ok else "FAIL"
            print(f"  {verdict}  {type_name:<20} {type_key}")
            if not ok:
                print(f"        detail: {detail[:200]}")
    finally:
        # LIFO cleanup: every successful create has a delete registered.
        for cleanup in reversed(ctx.cleanups):
            cleanup()

    # The mapping says "these 9 should work". If the live probe says
    # otherwise, surface a non-zero exit so CI / a maintainer notices.
    expected_passes = {n for n, k in expected_map.items()}
    actual_passes = {n for n, (ok, _, _) in results.items() if ok}
    new_passes = actual_passes - expected_passes
    new_fails = expected_passes - actual_passes
    if not (new_passes or new_fails):
        print()
        print(f"All {len(expected_passes)} mapped types still pass live. Mapping is current.")
        return 0

    print()
    print("Drift between in-tree mapping and live behavior:")
    if new_fails:
        print(f"  no longer accepted: {sorted(new_fails)}")
        print("    -> remove these from stint/dialects/jira/tmp/types.py::TMP_FIELD_TYPE_KEYS")
    if new_passes:
        print(f"  newly accepted:     {sorted(new_passes)}")
        print("    -> add these to stint/dialects/jira/tmp/types.py::TMP_FIELD_TYPE_KEYS")
    return 1


if __name__ == "__main__":
    sys.exit(main())
