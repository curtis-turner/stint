"""Shared respx stub helpers for the Jira admin endpoints newer features read.

Kept out of conftest.py so tests import it explicitly and the mock set stays
visible at each callsite.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import respx


def paginated(values: list[Any]) -> dict[str, Any]:
    """Single-page PageBean shape used by Jira admin endpoints."""
    return {"values": values, "isLast": True, "startAt": 0, "maxResults": len(values)}


def stub_project_roles_and_permission_schemes(mock: respx.MockRouter, root: str) -> None:
    """Empty stubs for the role-actor and permission-scheme reflect reads.

    - ``GET /role`` (global role map) → empty page
    - ``GET /project/{id}`` → minimal project (lead fetch)
    - ``GET /project/{id}/role`` → empty role-name → URL map
    - ``GET /project/{id}/role/{roleId}`` → role with no actors
    - ``GET /project/{id}/permissionscheme`` → the default scheme
    - ``GET /permissionscheme`` (and ``?expand=permissions``) → empty list
    """
    esc = re.escape(root)
    mock.get(f"{root}/role").mock(return_value=httpx.Response(200, json=paginated([])))
    mock.get(url__regex=rf"^{esc}/project/[^/]+$").mock(
        return_value=httpx.Response(200, json={"id": "10000", "key": "P", "name": "P"})
    )
    mock.get(url__regex=rf"^{esc}/project/[^/]+/role$").mock(return_value=httpx.Response(200, json={}))
    mock.get(url__regex=rf"^{esc}/project/[^/]+/role/[^/]+$").mock(
        return_value=httpx.Response(200, json={"actors": []})
    )
    mock.get(url__regex=rf"^{esc}/project/[^/]+/permissionscheme$").mock(
        return_value=httpx.Response(200, json={"id": "10000", "name": "Default Permission Scheme"})
    )
    mock.get(url__regex=rf"^{esc}/permissionscheme($|\?)").mock(
        return_value=httpx.Response(200, json={"permissionSchemes": []})
    )
