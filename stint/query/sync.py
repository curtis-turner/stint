"""Synchronous facade over AsyncSession for users who don't want asyncio.

  from stint import Session, create_engine, StateFile, PATAuth, select

  state = StateFile.load("state/dev.yaml")
  engine = create_engine("jira_cloud+https://...", auth=APITokenAuth(email, token))

  with Session(engine, state) as session:
      bug = session.get(Bug, "PLAT-1")
      bug.severity = "S1"
      session.commit()

Session owns a private asyncio event loop and runs AsyncSession on it. The
engine's httpx client binds to that loop on first I/O, so by default closing
the Session also closes the engine — pass ``close_engine=False`` to keep the
engine alive (rare: typically the next Session needs a fresh engine anyway,
since the http client cannot outlive its loop).

Refuses to construct inside a running event loop. Inside async code, use
AsyncSession directly.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from stint.query.session import AsyncSession, CommitResult

if TYPE_CHECKING:
    from stint.engine import Engine
    from stint.query.select import Select
    from stint.schema.issuetype import IssueType
    from stint.state.file import StateFile

M = TypeVar("M", bound="IssueType")


class Session:
    """Sync facade. Same surface as AsyncSession minus the ``await``."""

    def __init__(
        self,
        engine: Engine,
        state: StateFile,
        *,
        close_engine: bool = True,
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "Session cannot be created inside a running event loop. Use AsyncSession directly in async code."
            )
        self._loop = asyncio.new_event_loop()
        self._async = AsyncSession(engine, state)
        self._engine = engine
        self._close_engine = close_engine
        self._closed = False

    # ── Pass-through accessors ──────────────────────────────────────
    @property
    def engine(self) -> Engine:
        return self._async.engine

    @property
    def state(self) -> StateFile:
        return self._async.state

    # ── Reads ────────────────────────────────────────────────────────
    def get(self, model: type[M], key: str) -> M | None:
        return self._loop.run_until_complete(self._async.get(model, key))

    def scalars(self, stmt: Select[M]) -> list[M]:
        return self._loop.run_until_complete(self._async.scalars(stmt))

    # ── Writes ───────────────────────────────────────────────────────
    def add(self, instance: IssueType, *, project: str | None = None) -> None:
        self._async.add(instance, project=project)

    def delete(self, instance: IssueType) -> None:
        self._async.delete(instance)

    def commit(self) -> list[CommitResult]:
        return self._loop.run_until_complete(self._async.commit())

    # ── Lifecycle ────────────────────────────────────────────────────
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._close_engine:
                self._loop.run_until_complete(self._engine.close())
        finally:
            self._loop.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
