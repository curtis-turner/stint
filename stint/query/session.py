"""Async session for reading and writing Jira issues as Pydantic instances.

  async with AsyncSession(engine, state) as session:
      # READ
      bugs = await session.scalars(
          select(Bug).where(Bug.c.severity == "S1")
      )
      bug = await session.get(Bug, "PLAT-123")

      # WRITE
      session.add(Bug(summary="boom", reporter="alice", severity="S1"))
      bug.severity = "S2"           # tracked, dirty
      session.delete(other_bug)
      await session.commit()

Identity map: repeated lookups of the same (Model, issue_key) return the
same Python object within a session. After ``commit()`` an inserted instance
gets its newly-assigned ``key`` set and joins the identity map.

Dirty tracking: when ``get()``/``scalars()`` hydrates an instance, the session
captures ``instance.model_dump()`` as a baseline. On commit, only fields that
differ from the baseline are sent in the UPDATE PUT.

Commit ordering: inserts first, then updates, then deletes. If any operation
fails, ``PartialCommitError`` is raised carrying per-instance results.
Successful work is NOT rolled back (Jira admin REST has no transactions).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from stint.exceptions import ConfigurationError, PartialCommitError
from stint.query.hydrate import field_keys_for_model, hydrate
from stint.query.payload import (
    build_insert_payload,
    build_update_payload,
)

if TYPE_CHECKING:
    from stint.engine import Engine
    from stint.query.select import Select
    from stint.schema.issuetype import IssueType
    from stint.state.file import StateFile


@dataclass
class CommitResult:
    """Outcome of one operation within a commit batch."""

    instance: Any
    operation: Literal["insert", "update", "delete"]
    success: bool
    error: Exception | None = None


class AsyncSession:
    """Read + write session."""

    def __init__(self, engine: Engine, state: StateFile) -> None:
        self.engine = engine
        self.state = state
        self._identity: dict[tuple[type, str], IssueType] = {}
        self._originals: dict[tuple[type, str], dict[str, Any]] = {}
        self._pending_inserts: list[tuple[IssueType, str]] = []
        # (instance, project_key) — project resolved at add time
        self._pending_deletes: list[IssueType] = []

    async def __aenter__(self) -> AsyncSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        # Engine ownership belongs to the caller.
        return None

    # ── Reads ────────────────────────────────────────────────────────
    async def scalars(self, stmt: Select) -> list[IssueType]:
        jql = stmt.compile(self.state)
        fields = field_keys_for_model(stmt.model, self.state)
        results: list[IssueType] = []
        async for issue in self.engine.dialect.search(
            jql=jql,
            fields=fields,
            page_size=stmt.limit_n or 50,
        ):
            results.append(self._hydrate_with_identity(stmt.model, issue))
            if stmt.limit_n is not None and len(results) >= stmt.limit_n:
                break
        return results

    async def get(
        self,
        model: type[IssueType],
        key: str,
    ) -> IssueType | None:
        cached = self._identity.get((model, key))
        if cached is not None:
            return cached
        from stint.exceptions import NotFoundError

        fields = field_keys_for_model(model, self.state)
        try:
            issue = await self.engine.dialect.get_issue(key, fields=fields)
        except NotFoundError:
            return None
        return self._hydrate_with_identity(model, issue)

    # ── Writes ───────────────────────────────────────────────────────
    def add(self, instance: IssueType, *, project: str | None = None) -> None:
        """Stage `instance` for insertion on the next commit.

        ``project`` is the Jira project key. If omitted, inferred from
        ``instance.__class__.__projects__`` (set by ProjectMeta when exactly
        one Project lists this issuetype in ``__issuetypes__``).
        """
        if getattr(instance, "key", None):
            raise ConfigurationError(
                f"add: instance already has key={instance.key!r}. To track an "
                f"existing issue, hydrate it via session.get(); only NEW "
                f"instances (key=None) belong to add()."
            )
        project_key = project or _infer_project(instance)
        self._pending_inserts.append((instance, project_key))

    def delete(self, instance: IssueType) -> None:
        """Stage `instance` for deletion on commit. Requires `instance.key`."""
        if not getattr(instance, "key", None):
            raise ConfigurationError("delete: instance has no `key`; cannot delete an unsaved issue.")
        self._pending_deletes.append(instance)

    async def commit(self) -> list[CommitResult]:
        """Flush pending writes. Returns per-instance results.

        Raises ``PartialCommitError`` if ANY operation fails (the exception
        carries the full result list). Successful work is not rolled back.
        """
        is_cloud = self._is_cloud()
        results: list[CommitResult] = []

        # 1. Inserts (in add() order)
        for instance, project_key in self._pending_inserts:
            try:
                body = build_insert_payload(
                    instance,
                    self.state,
                    is_cloud=is_cloud,
                    project_key=project_key,
                )
                response = await self.engine.dialect.create_issue(body)
                new_key = response.get("key")
                if new_key:
                    instance.key = new_key
                    self._identity[(type(instance), new_key)] = instance
                    self._originals[(type(instance), new_key)] = instance.model_dump()
                results.append(
                    CommitResult(
                        instance=instance,
                        operation="insert",
                        success=True,
                    )
                )
            except Exception as e:
                results.append(
                    CommitResult(
                        instance=instance,
                        operation="insert",
                        success=False,
                        error=e,
                    )
                )
        self._pending_inserts.clear()

        # 2. Updates (dirty-tracked instances)
        for key_tuple, instance in list(self._identity.items()):
            dirty = self._dirty_fields(instance, key_tuple)
            if not dirty:
                continue
            try:
                body = build_update_payload(
                    instance,
                    self.state,
                    is_cloud=is_cloud,
                    dirty=dirty,
                )
                await self.engine.dialect.update_issue(key_tuple[1], body)
                self._originals[key_tuple] = instance.model_dump()
                results.append(
                    CommitResult(
                        instance=instance,
                        operation="update",
                        success=True,
                    )
                )
            except Exception as e:
                results.append(
                    CommitResult(
                        instance=instance,
                        operation="update",
                        success=False,
                        error=e,
                    )
                )

        # 3. Deletes
        for instance in self._pending_deletes:
            key = instance.key
            if key is None:  # guarded at delete(); defensive
                continue
            try:
                await self.engine.dialect.delete_issue(key)
                key_tuple = (type(instance), key)
                self._identity.pop(key_tuple, None)
                self._originals.pop(key_tuple, None)
                results.append(
                    CommitResult(
                        instance=instance,
                        operation="delete",
                        success=True,
                    )
                )
            except Exception as e:
                results.append(
                    CommitResult(
                        instance=instance,
                        operation="delete",
                        success=False,
                        error=e,
                    )
                )
        self._pending_deletes.clear()

        if any(not r.success for r in results):
            raise PartialCommitError(results)
        return results

    # ── Helpers ──────────────────────────────────────────────────────
    def _hydrate_with_identity(
        self,
        model: type[IssueType],
        issue: dict[str, Any],
    ) -> IssueType:
        key = issue.get("key")
        if key is not None:
            cached = self._identity.get((model, key))
            if cached is not None:
                return cached
        instance = hydrate(model, issue, self.state)
        if key is not None:
            self._identity[(model, key)] = instance
            self._originals[(model, key)] = instance.model_dump()
        return instance

    def _dirty_fields(
        self,
        instance: IssueType,
        key_tuple: tuple[type, str],
    ) -> set[str]:
        baseline = self._originals.get(key_tuple)
        if baseline is None:
            return set()
        current = instance.model_dump()
        return {attr for attr, value in current.items() if baseline.get(attr) != value and attr != "key"}

    def _is_cloud(self) -> bool:
        return self.engine.dialect.name == "jira_cloud"


def _infer_project(instance: IssueType) -> str:
    """Look up `instance.__class__.__projects__` set by ProjectMeta."""
    model = type(instance)
    projects = getattr(model, "__projects__", ())
    if not projects:
        raise ConfigurationError(
            f"add: cannot infer project for {model.__name__!r}. No declared "
            f"Project includes this issuetype in __issuetypes__. Pass "
            f'`project="<KEY>"` explicitly.'
        )
    if len(projects) > 1:
        raise ConfigurationError(
            f"add: {model.__name__!r} is declared in multiple projects "
            f'({list(projects)}). Pass `project="<KEY>"` explicitly.'
        )
    return projects[0]
