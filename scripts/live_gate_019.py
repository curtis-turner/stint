"""Live-tenant gate for 0.19.0 against the configured env (.stint/devel.yaml).

Runs, in order:

1. ``engine.reflect()`` — role-actor reflection, permission-scheme grants,
   project bindings.
2. ``engine.check(SchemaProject, state)`` — the drift + cosmetic-lock
   rules against live data.
3. A dry ``diff`` — what a fresh ``stint revision --autogenerate`` would
   emit right now (expect: zero changes if state is in sync).

Credentials come from the environment (STINT_USER / STINT_TOKEN, or the
sourced .stint/verify.env). Read-only: reflect and check never write.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from stint import StateFile, create_engine


async def main() -> int:
    import examples.company_managed.security_scheme as schema_module  # noqa: F401
    from stint import op  # noqa: F401
    from stint.autogen.desired import build_desired_snapshot
    from stint.autogen.diff import diff

    state_path = Path("examples/company_managed/state/devel.yaml")
    state = StateFile.from_yaml(state_path.read_text())

    env_path = Path(".stint/devel.yaml")
    url = None
    for line in env_path.read_text().splitlines():
        if line.strip().startswith("url:"):
            url = line.split(":", 1)[1].strip()
            break
    assert url and url.startswith("jira_cloud+"), f"unexpected url in {env_path}: {url!r}"

    user = os.environ.get("STINT_USER")
    token = os.environ.get("STINT_TOKEN")
    if not user or not token:
        print("STINT_USER / STINT_TOKEN not set; source .stint/verify.env first.")
        return 2

    from stint import APITokenAuth

    engine = create_engine(url, auth=APITokenAuth(user, token))
    async with engine:
        print("=== reflect ===")
        snap = await engine.reflect()
        for key, proj in snap.projects.items():
            print(f"project {key}: id={proj.id} perm_scheme={proj.permission_scheme_id}")
            for role_name, actors in proj.roles.items():
                print(f"  role {role_name}: {[f'{a.type}:{a.parameter}' for a in actors]}")
        for sid, scheme in snap.permission_schemes.items():
            print(f"permission scheme {sid} {scheme.name!r}: {len(scheme.grants)} grants")
            for g in scheme.grants:
                print(f"  {g.permission} -> {g.holder_type}:{g.holder_parameter}")

        print("=== engine.check ===")
        project_cls = next(
            cls
            for cls in vars(schema_module).values()
            if isinstance(cls, type) and getattr(cls, "__key__", None) == "SEC"
        )
        report = await engine.check(project_cls, state)
        for err in report.errors:
            print(f"ERROR: {err}")
        for warn in report.warnings:
            print(f"WARN: {warn}")
        print(f"check: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")

        print("=== diff (dry) ===")
        desired = build_desired_snapshot()
        result = diff(desired=desired, snapshot=snap, state=state, allow_delete=False)
        for change in result.changes:
            print(f"would change: {type(change).__name__}")
        for warn in result.warnings:
            print(f"diff WARN: {warn}")
        print(f"diff: {len(result.changes)} change(s), {len(result.warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
