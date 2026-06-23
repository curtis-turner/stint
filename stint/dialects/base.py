"""Dialect protocol. Every backend must satisfy this contract.

M1 first slice exposes only the read-side surface: detect() and reflect().
Write-side methods (compile_select, fetch, get_by_pk, insert, update, delete,
normalize_in/out) come in M2-M7.
"""

from __future__ import annotations

from typing import Protocol

from stint.state.snapshot import Snapshot


class Dialect(Protocol):
    """The contract a backend dialect must satisfy."""

    name: str

    async def detect(self) -> bool:
        """Probe the backend and return True if this dialect matches it.

        Used for auto-detection when the user did not specify a dialect at
        engine creation. The implementation should make a single cheap call
        (e.g. `GET /serverInfo`) and inspect the response.
        """

    async def reflect(self) -> Snapshot:
        """Read the current admin state from the backend into a Snapshot.

        The Snapshot is dialect-agnostic. The planner consumes it without
        needing to know which backend produced it.
        """
