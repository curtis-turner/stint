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

Workflow transitions live outside commit(): ``transition()`` POSTs
immediately, by target status name (case-insensitive) or raw transition id.
Moving to the current status is a no-op, so sync loops stay idempotent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeVar, cast

from stint.exceptions import (
    ConfigurationError,
    NotFoundError,
    PartialCommitError,
    TransitionError,
    TransportError,
)
from stint.fields import CustomField, MultiUserField, UserField
from stint.query import adf
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

M = TypeVar("M", bound="IssueType")


@dataclass
class CommitResult:
    """Outcome of one operation within a commit batch."""

    instance: Any
    operation: Literal["insert", "update", "delete"]
    success: bool
    error: Exception | None = None


class Transition(NamedTuple):
    """One available workflow transition out of an issue's current status.

    ``to_status`` is the target status name; unpacks as ``(id, name,
    to_status)`` for quick scans.
    """

    id: str
    name: str
    to_status: str


@dataclass
class Comment:
    """One issue comment, with the body rendered to plain text so callers
    can deduplicate ("don't post the same nag twice")."""

    id: str
    author: str
    created: str
    body_text: str


@dataclass
class ChangeItem:
    """One field change from the issue's changelog.

    ``field`` is the attribute name where possible: custom field ids are
    mapped back to their declared aliases when the state file knows them
    (``customfield_10044`` -> ``bug_severity``), otherwise the raw id or
    system field name. ``from_value``/``to_value`` prefer the human-readable
    strings Jira records; a fresh value has ``from_value=None``.
    """

    created: str
    author: str
    field: str
    from_value: str | None
    to_value: str | None


@dataclass
class Attachment:
    """One issue attachment, as Jira reports it."""

    id: str
    filename: str
    size: int
    created: str


@dataclass
class IssueLink:
    """One entry of the issue's ``fields.issuelinks``.

    ``direction`` is relative to the issue that was read: ``"outward"``
    means the issue is the subject (A in "A blocks B"), ``"inward"`` the
    object ("B is blocked by A"). ``type`` is the link type's name.
    """

    id: str
    type: str
    direction: Literal["outward", "inward"]
    other_key: str


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
    async def scalars(self, stmt: Select[M]) -> list[M]:
        jql = stmt.compile(self.state)
        fields = field_keys_for_model(stmt.model, self.state)
        results: list[M] = []
        if stmt.limit_n == 0:
            return results
        # Jira's documented maxResults cap for issue search is 100; a higher
        # limit would rely on undocumented server clamping, so cap the page.
        page_size = min(stmt.limit_n or 50, 100)
        async for issue in self.engine.dialect.search(
            jql=jql,
            fields=fields,
            page_size=page_size,
        ):
            results.append(self._hydrate_with_identity(stmt.model, issue))
            if stmt.limit_n is not None and len(results) >= stmt.limit_n:
                break
        return results

    async def get(
        self,
        model: type[M],
        key: str,
        *,
        fresh: bool = False,
    ) -> M | None:
        """Read one issue by key.

        By default the identity map serves an instance already known to this
        session — including one just inserted, whose server-side state has
        not been re-read. Pass ``fresh=True`` to bypass the cache and verify
        against Jira (a create can 201 yet be immediately invisible to the
        same token under issue-level security).
        """
        cached = cast("M | None", self._identity.get((model, key)))
        if cached is not None and not fresh:
            return cached
        fields = field_keys_for_model(model, self.state)
        try:
            issue = await self.engine.dialect.get_issue(key, fields=fields)
        except NotFoundError:
            return None
        existing = self._identity.get((model, key))
        if fresh and existing is not None:
            # Refresh in place: callers hold a reference to the inserted
            # instance (e.g. to mutate and commit), so don't swap it out.
            # Hydrate a throwaway — _hydrate_with_identity would return the
            # cached object itself, making the copy a no-op.
            hydrated = hydrate(model, issue, self.state)
            for name in model.model_fields:
                setattr(existing, name, getattr(hydrated, name))
            self._originals[(model, key)] = existing.model_dump()
            return cast("M", existing)
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
                # Emails in user-typed fields resolve first (reporter is
                # stripped from inserts, so it never needs resolving).
                insert_attrs = {name for name in type(instance).model_fields if name not in ("key", "reporter")}
                await self._resolve_user_typed_values(instance, only=insert_attrs)
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
                await self._resolve_user_typed_values(instance, only=dirty)
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

    # ── Workflow transitions ─────────────────────────────────────────
    @staticmethod
    def _issue_key_of(instance: IssueType | str) -> str:
        if isinstance(instance, str):
            return instance
        key = getattr(instance, "key", None)
        if not key:
            raise ConfigurationError(
                "transition: instance has no `key`. Hydrate it via session.get() or pass the issue key string directly."
            )
        return str(key)

    async def available_transitions(self, issue: IssueType | str) -> list[Transition]:
        """List the workflow transitions available on the issue right now.

        Accepts a hydrated instance or a bare issue key. Returns
        ``(id, name, to_status)`` triples; ``to_status`` is the target
        status name.
        """
        key = self._issue_key_of(issue)
        return [
            Transition(
                id=str(t.get("id", "")),
                name=str(t.get("name", "")),
                to_status=str((t.get("to") or {}).get("name", "")),
            )
            for t in await self.engine.dialect.get_transitions(key)
        ]

    async def transition(
        self,
        issue: IssueType | str,
        *,
        to_status: str | None = None,
        transition_id: str | None = None,
        resolution: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Move the issue through its workflow.

        Address the destination by target status name (case-insensitive) via
        ``to_status=`` — the common sync case — or by raw transition id via
        ``transition_id=``. Passing the issue's current status as ``to_status``
        is a no-op, so sync loops stay idempotent.

        ``resolution=`` names the resolution to set (e.g. "Fixed") on
        transitions whose screen asks for one; ``fields=`` carries any other
        transition-screen fields as raw Jira JSON. Both ride in the same
        POST as the transition.

        On success a hydrated instance's ``status``/``status_category``
        attributes (when declared) are refreshed in place, and the session's
        dirty-tracking baseline is updated so a later commit() does not PUT
        the old status back.
        """
        if to_status is None and transition_id is None:
            raise ConfigurationError("transition: pass to_status= or transition_id=.")
        if to_status is not None and transition_id is not None:
            raise ConfigurationError("transition: pass to_status= or transition_id=, not both.")

        key = self._issue_key_of(issue)

        # Idempotency: moving to the current status is a no-op. Only possible
        # to detect when the instance carries a hydrated `status` attribute.
        if to_status is not None and not isinstance(issue, str):
            current = getattr(issue, "status", None)
            if isinstance(current, str) and current.lower() == to_status.lower():
                return

        transitions = await self.engine.dialect.get_transitions(key)
        chosen: dict[str, Any] | None = None
        if transition_id is not None:
            chosen = next((t for t in transitions if str(t.get("id")) == str(transition_id)), None)
            if chosen is None:
                raise TransitionError(
                    f"transition: issue {key} has no transition id {transition_id!r}. "
                    f"Available: {_describe_transitions(transitions)}"
                )
        else:
            assert to_status is not None
            chosen = next(
                (t for t in transitions if str((t.get("to") or {}).get("name", "")).lower() == to_status.lower()),
                None,
            )
            if chosen is None:
                raise TransitionError(
                    f"transition: no workflow transition from the current status "
                    f"of {key} leads to {to_status!r}. Available target statuses: "
                    f"{_describe_transitions(transitions)}"
                )

        body_fields: dict[str, Any] = dict(fields or {})
        if resolution is not None:
            body_fields.setdefault("resolution", {"name": resolution})
        body: dict[str, Any] = {"transition": {"id": str(chosen["id"])}}
        if body_fields:
            body["fields"] = body_fields

        await self.engine.dialect.perform_transition(key, body)

        # Refresh the in-memory instance so callers reading `bug.status` after
        # a transition see the new state, without marking it dirty.
        if not isinstance(issue, str):
            to_status_name = str((chosen.get("to") or {}).get("name", "")) or None
            category = ((chosen.get("to") or {}).get("statusCategory") or {}).get("key")
            self._refresh_after_transition(issue, key, to_status_name, category)

    def _refresh_after_transition(
        self,
        instance: IssueType,
        key: str,
        to_status: str | None,
        status_category: Any,
    ) -> None:
        """Mirror the transition's target status onto a hydrated instance.

        Only touches attributes the model declares, and only adjusts the
        dirty-tracking baseline for exactly those attributes — other pending
        mutations must stay dirty for the next commit().
        """
        key_tuple = (type(instance), key)
        baseline = self._originals.get(key_tuple)
        if baseline is None:
            return  # not session-managed (user-constructed instance or str key path)
        model_fields = type(instance).model_fields
        # Loop-setattr because the concrete attributes are subclass-declared
        # (the base IssueType does not carry them) and the `in model_fields`
        # guards keep pydantic from seeing an unknown field.
        for attr, value in (("status", to_status), ("status_category", status_category)):
            if attr in model_fields and value is not None:
                setattr(instance, attr, value)
                baseline[attr] = value

    # ── Comments ─────────────────────────────────────────────────────
    async def add_comment(self, issue: IssueType | str, body: str | dict[str, Any]) -> Comment:
        """Post a comment and return the created :class:`Comment`.

        Plain-text bodies are wrapped in ADF on Jira Cloud (reusing
        ``stint.query.adf``); a dict body is taken as raw ADF and sent
        as-is. Accepts a hydrated instance or a bare issue key.
        """
        key = self._issue_key_of(issue)
        payload: dict[str, Any] = body if isinstance(body, dict) else adf.wrap_plain_text(str(body))
        created = await self.engine.dialect.add_comment(key, payload)
        return _comment_from_payload(created)

    async def comments(self, issue: IssueType | str) -> list[Comment]:
        """List the issue's comments, paginated, plain-text rendered."""
        key = self._issue_key_of(issue)
        return [_comment_from_payload(c) async for c in self.engine.dialect.get_comments(key)]

    async def delete_comment(self, issue: IssueType | str, comment_id: str) -> None:
        """Delete one comment by id. Accepts a hydrated instance or a key."""
        key = self._issue_key_of(issue)
        await self.engine.dialect.delete_comment(key, comment_id)

    # ── Changelog ────────────────────────────────────────────────────
    async def changelog(self, issue: IssueType | str) -> AsyncIterator[ChangeItem]:
        """Yield the issue's field changes, oldest page first, paginated.

        Custom field ids are mapped back to their declared aliases where the
        state file knows them, so audit output speaks the schema's language.
        One entry per changed field per change event.
        """
        key = self._issue_key_of(issue)
        id_to_alias = {m.id: alias for alias, m in self.state.custom_fields.items()}
        async for entry in self.engine.dialect.get_changelog(key):
            author = entry.get("author") or {}
            author_name = str(author.get("displayName") or author.get("accountId") or "")
            created = str(entry.get("created", ""))
            for item in entry.get("items") or []:
                if not isinstance(item, dict):
                    continue
                field_name = str(item.get("field", ""))
                # Jira Cloud changelog items carry the display name in
                # ``field`` ("Severity") and the id in ``fieldId``
                # ("customfield_10044") — ``field`` never starts with
                # customfield_ there, so the id lookup is the only one that
                # fires on Cloud. ``field`` itself holds the raw id on some
                # payloads (older Data Center), and stays the display name
                # for system fields and unknown ids.
                alias = id_to_alias.get(str(item.get("fieldId") or "")) or id_to_alias.get(field_name)
                if alias is not None:
                    field_name = alias
                yield ChangeItem(
                    created=created,
                    author=author_name,
                    field=field_name,
                    from_value=_change_value(item, "from"),
                    to_value=_change_value(item, "to"),
                )

    # ── Attachments ──────────────────────────────────────────────────
    async def attach(
        self,
        issue: IssueType | str,
        file: str | Path | bytes,
        *,
        filename: str | None = None,
    ) -> Attachment:
        """Upload one attachment. Returns the created :class:`Attachment`.

        ``file`` is a filesystem path or raw bytes; bytes require
        ``filename=`` (there is no name to infer). Server-side size limits
        and errors surface as StintError subclasses, like every transport
        failure.
        """
        key = self._issue_key_of(issue)
        data, name = _coerce_attachment(file, filename)
        raw = await self.engine.dialect.attach(key, data=data, filename=name)
        if isinstance(raw, list):  # defensive: some tenants return a 1-element array
            raw = raw[0] if raw else {}
        return _attachment_from_payload(raw)

    async def attachments(self, issue: IssueType | str) -> list[Attachment]:
        """List the issue's attachments (one GET, no extra request per file)."""
        key = self._issue_key_of(issue)
        return [_attachment_from_payload(a) for a in await self.engine.dialect.get_attachments(key)]

    async def download_attachment(self, attachment_id: str) -> bytes:
        """Fetch one attachment's content by id, as raw bytes."""
        return await self.engine.dialect.download_attachment(attachment_id)

    async def delete_attachment(self, attachment_id: str) -> None:
        """Delete one attachment by id."""
        await self.engine.dialect.delete_attachment(attachment_id)

    async def replace_attachment(
        self,
        issue: IssueType | str,
        file: str | Path | bytes,
        *,
        filename: str | None = None,
    ) -> Attachment:
        """Regenerate-in-place helper: delete any existing attachment with
        the same filename, then upload the new one.

        Mirrors the sync-service pattern of a CSV export re-uploaded each
        time its upstream records change — without it, every pass would
        stack another ``export.csv`` on the ticket.
        """
        data, name = _coerce_attachment(file, filename)
        for existing in await self.attachments(issue):
            if existing.filename == name:
                await self.delete_attachment(existing.id)
        return await self.attach(issue, data, filename=name)

    # ── Issue links ──────────────────────────────────────────────────
    async def link(self, source: IssueType | str, target: IssueType | str, *, link_type: str) -> None:
        """Create a typed link between two issues: ``source`` is the outward
        subject (A in "A blocks B"), ``target`` the inward object.

        Link types are addressed by name; stint resolves the direction
        bookkeeping. Creating a link that already exists (same type, same
        direction, same pair) is a no-op — checked locally before the
        request, with the race tolerated server-side — so sync loops can
        re-derive dependencies every pass. Accepts instances or keys.
        """
        source_key = self._issue_key_of(source)
        target_key = self._issue_key_of(target)
        if source_key.lower() == target_key.lower():
            raise ConfigurationError(f"link: cannot link {source_key} to itself.")

        for existing in await self.links(source_key):
            if (
                existing.type.lower() == link_type.lower()
                and existing.direction == "outward"
                and existing.other_key.lower() == target_key.lower()
            ):
                return  # already there: idempotent no-op

        try:
            await self.engine.dialect.create_issue_link(
                link_type=link_type,
                outward_key=source_key,
                inward_key=target_key,
            )
        except TransportError as exc:
            # Lost the race with another writer: Jira 400s a duplicate pair.
            # "already ..." in the body is the same state we pre-checked, so
            # treat it as done (same tolerance as add_project_role_actor).
            if exc.status_code == 400 and "already" in (exc.body or "").lower():
                return
            raise

    async def links(self, issue: IssueType | str) -> list[IssueLink]:
        """List the issue's links, one GET total (not one per link)."""
        key = self._issue_key_of(issue)
        out: list[IssueLink] = []
        for entry in await self.engine.dialect.get_issuelinks(key):
            link_type = str((entry.get("type") or {}).get("name", ""))
            other: dict[str, Any] | None
            if entry.get("outwardIssue") is not None:
                direction = "outward"
                other = entry["outwardIssue"]
            elif entry.get("inwardIssue") is not None:
                direction = "inward"
                other = entry["inwardIssue"]
            else:
                continue
            out.append(
                IssueLink(
                    id=str(entry.get("id", "")),
                    type=link_type,
                    direction=direction,  # type: ignore[arg-type]
                    other_key=str((other or {}).get("key", "")),
                )
            )
        return out

    async def unlink(self, link_id: str) -> None:
        """Delete one link by id (as returned by :meth:`links`)."""
        await self.engine.dialect.delete_issue_link(link_id)

    # ── Helpers ──────────────────────────────────────────────────────
    async def _resolve_user_typed_values(self, instance: IssueType, *, only: set[str]) -> None:
        """Resolve emails to platform user ids on user-typed attributes,
        in place, before payload building.

        Covers the system user fields (assignee, reporter, creator) and
        custom ``UserField``/``MultiUserField`` attributes. Lookups go
        through the dialect's resolver, which caches per engine — one
        user-search per email no matter how many issues name the same
        person. Failures raise ConfigurationError naming the model,
        attribute and email; the commit's per-instance try treats that like
        any other write failure (PartialCommitError, nothing silently
        dropped).
        """
        model = type(instance)
        for attr_name, field_info in model.model_fields.items():
            if attr_name == "key" or attr_name not in only:
                continue
            value = getattr(instance, attr_name, None)
            if value is None:
                continue
            cf = _custom_field_meta(field_info)
            if cf is not None:
                user_typed = cf.type in (UserField, MultiUserField)
            else:
                user_typed = attr_name in ("assignee", "reporter", "creator")
            if not user_typed:
                continue
            if isinstance(value, str) and "@" in value:
                try:
                    resolved = await self.engine.dialect.resolve_user(value)
                except ConfigurationError as exc:
                    raise ConfigurationError(f"{model.__name__}.{attr_name}: {exc}") from exc
                setattr(instance, attr_name, resolved)
            elif isinstance(value, list):
                resolved_items = []
                changed = False
                for item in value:
                    if isinstance(item, str) and "@" in item:
                        try:
                            resolved_items.append(await self.engine.dialect.resolve_user(item))
                        except ConfigurationError as exc:
                            raise ConfigurationError(f"{model.__name__}.{attr_name}: {exc}") from exc
                        changed = True
                    else:
                        resolved_items.append(item)
                if changed:
                    setattr(instance, attr_name, resolved_items)

    def _hydrate_with_identity(
        self,
        model: type[M],
        issue: dict[str, Any],
    ) -> M:
        key = issue.get("key")
        if key is not None:
            cached = cast("M | None", self._identity.get((model, key)))
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


def _comment_from_payload(raw: dict[str, Any]) -> Comment:
    """Project a Jira comment payload onto the plain-text :class:`Comment`."""
    body = raw.get("body")
    author = raw.get("author") or {}
    return Comment(
        id=str(raw.get("id", "")),
        author=str(author.get("displayName") or author.get("accountId") or ""),
        created=str(raw.get("created", "")),
        body_text=adf.parse_text(body) if isinstance(body, dict) else str(body or ""),
    )


def _change_value(item: dict[str, Any], side: Literal["from", "to"]) -> str | None:
    """Human-readable value first (``fromString``/``toString``), raw id
    second, None when the field had no value on that side."""
    readable = item.get(f"{side}String")
    if readable is not None:
        return str(readable)
    raw = item.get(side)
    return str(raw) if raw is not None else None


def _custom_field_meta(field_info: Any) -> CustomField | None:
    """The CustomField in a Pydantic Annotated metadata list, if any."""
    return next((m for m in field_info.metadata if isinstance(m, CustomField)), None)


def _attachment_from_payload(raw: dict[str, Any]) -> Attachment:
    """Project a Jira attachment payload onto :class:`Attachment`."""
    return Attachment(
        id=str(raw.get("id", "")),
        filename=str(raw.get("filename", "")),
        size=int(raw.get("size") or 0),
        created=str(raw.get("created", "")),
    )


def _coerce_attachment(file: str | Path | bytes, filename: str | None) -> tuple[bytes, str]:
    """Normalize attach() input to (bytes, filename). A path contributes its
    basename when no explicit filename is given; bytes require one."""
    if isinstance(file, bytes):
        if not filename:
            raise ConfigurationError(
                "attach: pass filename= when uploading raw bytes (there is no path to infer a name from)."
            )
        return file, filename
    path = Path(file)
    if not path.is_file():
        raise ConfigurationError(f"attach: no such file: {path}")
    return path.read_bytes(), filename or path.name


def _describe_transitions(transitions: list[dict[str, Any]]) -> str:
    """Render available transitions as 'status (via transition "name")' pairs."""
    if not transitions:
        return "(none)"
    return ", ".join(
        f"{(t.get('to') or {}).get('name', '?')!r} (via {t.get('name', '?')!r}, id {t.get('id', '?')})"
        for t in transitions
    )


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
