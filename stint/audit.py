"""Out-of-band write audit for screen-locked ("control") fields.

The restricted-Edit-screen pattern locks fields in the UI only. Since the
override-screen-security deprecation, Jira Cloud's REST API no longer checks
screens on edit (JRACLOUD-98054): any caller holding ``EDIT_ISSUES`` can
write a screen-locked field, and Rovo exposes that to every licensed user.
The permission scheme is the real gate; the screen is a behavioral one.

This module closes the loop the schema cannot express. It derives the
*control fields* a project declares — fields on the create/view screens but
absent from the edit screen — scans recent changelog entries for those
fields, and reports writes by identities outside an allow-list. A long-running
service can call ``engine.audit_control_fields`` at startup or on a schedule
and alert on anything it finds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from stint.exceptions import StintError
from stint.fields import CustomField
from stint.registry import registry

# System fields Jira never lets REST callers write (imports aside). They are
# on create/view screens by nature and off editable screens by nature, so
# they must not count as "locked" fields for the cosmetic-lock rule.
_IMMUTABLE_SYSTEM_FIELDS: frozenset[str] = frozenset({"created", "updated"})

if TYPE_CHECKING:
    from stint.engine import Engine
    from stint.schema.project import Project
    from stint.state.file import StateFile


@dataclass
class ControlFieldWrite:
    """One out-of-band write to a control field.

    ``field`` is the declared alias when the state file knows the custom
    field, else the raw changelog field name/id. ``author`` is the accountId
    (or display name when the payload omits it). ``reverted`` records
    whether a later changelog entry restored the previous value on the same
    issue and field (the guard's revert signature) — unreverted writes are
    the ones a service should alert on, as a backstop for the guard rule
    failing, being disabled, or hitting usage limits.
    """

    issue_key: str
    field: str
    author: str
    at: str  # Jira's ISO timestamp from the changelog entry
    from_value: str | None
    to_value: str | None
    reverted: bool = False


@dataclass
class ControlFieldAuditReport:
    """Findings of one audit run over a project."""

    project: str
    since: str  # the JQL filter applied (readable form)
    control_fields: tuple[str, ...]  # declared aliases / system field names
    allowed_writers: tuple[str, ...]  # as given (emails resolved internally)
    writes: list[ControlFieldWrite] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.writes

    @property
    def unreverted_writes(self) -> list[ControlFieldWrite]:
        return [w for w in self.writes if not w.reverted]

    def raise_for_writes(self) -> None:
        if not self.writes:
            return
        unreverted = self.unreverted_writes
        reverted_count = len(self.writes) - len(unreverted)
        suffix = f" ({reverted_count} of them were reverted by the guard)" if reverted_count else ""
        if not unreverted:
            return
        details = "\n".join(
            f"  - {w.at} {w.issue_key}: {w.field} by {w.author} ({w.from_value!r} -> {w.to_value!r})"
            for w in unreverted
        )
        raise StintError(
            f"unreverted out-of-band writes to control fields on project {self.project!r}{suffix}:\n{details}"
        )


def declared_control_fields(project_cls: type[Project]) -> dict[str, tuple[str, ...]]:
    """Issuetype label -> control fields declared for it.

    A control field is a field the schema puts on a project's create or view
    screen but leaves off the edit screen of the same ScreenScheme: the
    restricted-Edit-screen pattern. Comparison uses the declared reference
    identity (custom-field alias or system field name), which is consistent
    across the screens of one scheme by construction.

    Immutable system fields (``Created``, ``Updated``) are excluded: Jira
    refuses writes to them for every caller regardless of ``EDIT_ISSUES``,
    so they can never be cosmetic-lock candidates (verified live: a tenant
    whose only create/view-minus-edit fields are these two fired the
    cosmetic-lock warning against a wide-open scheme).
    """
    out: dict[str, tuple[str, ...]] = {}
    for it in getattr(project_cls, "__issuetypes__", ()) or ():
        scheme = getattr(it, "__screen_scheme__", None)
        if scheme is None:
            continue

        def _ref_key(ref: Any) -> str:
            return ref.alias if isinstance(ref, CustomField) else str(ref)

        editable = {_ref_key(ref) for ref in scheme.edit.fields}
        control: list[str] = []
        for screen in (scheme.create, scheme.view):
            for ref in screen.fields:
                key = _ref_key(ref)
                if key.lower() in _IMMUTABLE_SYSTEM_FIELDS:
                    continue
                if key not in editable and key not in control:
                    control.append(key)
        if control:
            out[getattr(it, "__alias__", it.__name__)] = tuple(control)
    return out


def _writer_label(writer: Any) -> str:
    """Readable allow-list entry for the report (``user:email``,
    ``group:name``, ``projectrole:name``)."""
    from stint.schema.roles import Group, User

    if isinstance(writer, User):
        return f"user:{writer.value}"
    if isinstance(writer, Group):
        return f"group:{writer.name}"
    if type(writer).__name__ == "ProjectRole":
        return f"projectrole:{writer.name}"
    return str(writer)


def _resolve_since(since: datetime | str) -> str:
    """JQL-readable form of the ``since`` bound."""
    if isinstance(since, str):
        return since
    return since.strftime("%Y/%m/%d %H:%M")


def _parse_jira_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _change_value(item: dict[str, Any], side: str) -> str | None:
    readable = item.get(f"{side}String")
    if readable is not None:
        return str(readable)
    raw = item.get(side)
    return str(raw) if raw is not None else None


async def allowed_writer_checker(
    dialect: Any,
    writers: list[Any],
    *,
    project_id: str | None = None,
) -> Any:
    """One shared allow-list for the guard-facing checks (``engine.check``
    drift detection and this audit).

    ``writers`` are typed declarations — ``User`` (email or accountId),
    ``Group`` (name), ``ProjectRole`` (name). Returns an async
    ``is_allowed(account_id)`` predicate: direct account match, group
    membership, or project-role membership (the latter only when
    ``project_id`` is given). This is the single allow-list both the check
    and the audit evaluate, so they can never disagree about who may write.
    """
    from stint.schema.roles import Group, User

    user_ids = {await dialect.resolve_user(w.value) for w in writers if isinstance(w, User)}
    group_names = {w.name for w in writers if isinstance(w, Group)}
    role_names = {w.name for w in writers if type(w).__name__ == "ProjectRole"}
    groups_cache: dict[str, frozenset[str]] = {}
    roles_cache: dict[str, set[str]] = {}

    async def is_allowed(account_id: str) -> bool:
        if account_id in user_ids:
            return True
        if group_names:
            if account_id not in groups_cache:
                groups_cache[account_id] = await dialect.user_groups(account_id)
            if groups_cache[account_id] & group_names:
                return True
        if role_names and project_id:
            if account_id not in roles_cache:
                roles_cache[account_id] = await dialect.project_role_memberships(project_id, account_id)
            if roles_cache[account_id] & role_names:
                return True
        return False

    return is_allowed


async def audit_control_fields(
    engine: Engine,
    project_cls: type[Project],
    state: StateFile,
    *,
    since: datetime | str,
    allowed_writers: list[str] | tuple[str, ...] = (),
    guard: Any = None,
) -> ControlFieldAuditReport:
    """Scan recent changelog entries for writes to declared control fields.

    ``since`` bounds the scan: a datetime (formatted into the JQL
    ``updated >= "..."`` filter, and compared against each changelog entry's
    timestamp) or a raw JQL-ready string. The allow-list comes from one of:

    - ``guard`` (preferred): a declared ``ControlFieldGuard`` — its
      ``allowed_writers`` (users, groups, project roles) are resolved
      through the same checker ``engine.check`` uses, so both surfaces
      share one allow-list.
    - ``allowed_writers``: emails or accountIds whose writes are expected
      (the service account, automation users); anything else touching a
      control field lands in the report.

    Each reported write is classified: ``reverted`` when a later changelog
    entry on the same issue and field restores the previous value (the
    guard's revert signature), ``unreverted`` otherwise.
    ``report.raise_for_writes`` alerts on the unreverted ones.

    Read-only: one JQL search plus changelog GETs per updated issue.
    """
    dialect = engine.dialect
    control_by_issuetype = declared_control_fields(project_cls)
    flat = tuple(sorted({name for names in control_by_issuetype.values() for name in names}))
    if guard is not None:
        writer_labels = [_writer_label(w) for w in guard.allowed_writers]
    else:
        writer_labels = list(allowed_writers)
    report = ControlFieldAuditReport(
        project=project_cls.__key__,
        since=_resolve_since(since),
        control_fields=flat,
        allowed_writers=tuple(writer_labels),
    )
    if not flat:
        return report

    # The allow-list. Guard mode uses the shared typed checker (with the
    # bound project's id so role membership is verifiable); plain-string
    # mode keeps the simple email/accountId/display-name set.
    if guard is not None:
        project_mapping = state.projects.get(project_cls.__key__)
        is_allowed = await allowed_writer_checker(
            dialect,
            list(guard.allowed_writers),
            project_id=project_mapping.id if project_mapping else None,
        )
    else:
        allowed_ids: set[str] = set()
        for value in allowed_writers:
            if "@" in value:
                allowed_ids.add(await dialect.resolve_user(value))
            else:
                allowed_ids.add(value)

        async def is_allowed(author_id: str) -> bool:
            return author_id in allowed_ids

    # alias resolution, same convention as the query plane's changelog:
    # changelog items carry the id in ``fieldId`` and the display name in
    # ``field``; map both back to declared aliases.
    alias_by_id = {m.id: alias for alias, m in state.custom_fields.items()}
    alias_by_name: dict[str, str] = {}
    for alias, cf in registry.custom_fields.items():
        alias_by_name[cf.name] = alias

    since_dt = None
    if isinstance(since, datetime):
        since_dt = since if since.tzinfo else since.replace(tzinfo=UTC)

    jql = f'project = "{project_cls.__key__}" AND updated >= "{_resolve_since(since)}"'
    async for issue in dialect.search(jql=jql, fields=["summary"]):
        key = str(issue.get("key", ""))
        if not key:
            continue
        async for entry in dialect.get_changelog(key):
            created = str(entry.get("created", ""))
            if since_dt is not None:
                entry_dt = _parse_jira_ts(created)
                if entry_dt is not None and entry_dt < since_dt:
                    continue
            author = entry.get("author") or {}
            author_id = str(author.get("accountId", "") or author.get("displayName") or "")
            if await is_allowed(author_id):
                continue
            for item in entry.get("items") or []:
                if not isinstance(item, dict):
                    continue
                field_id = str(item.get("fieldId", "") or "")
                field_name = str(item.get("field", "") or "")
                alias = alias_by_id.get(field_id) or alias_by_name.get(field_name)
                matched = (
                    alias
                    if (alias and alias in flat)
                    else (field_name if field_name in flat else (field_id if field_id in flat else None))
                )
                if matched is None:
                    continue
                report.writes.append(
                    ControlFieldWrite(
                        issue_key=key,
                        field=matched,
                        author=author_id,
                        at=created,
                        from_value=_change_value(item, "from"),
                        to_value=_change_value(item, "to"),
                    )
                )
        _classify_reverted(report.writes, key)
    # A revert entry is itself a control-field write by an unlisted author
    # (the guard actor is tenant-specific and not in the allow-list) — drop
    # the reverts now that each is matched to the violation it restored.
    report.writes = [w for w in report.writes if not _is_revert_entry(w, report.writes)]
    return report


def _is_revert_entry(w: ControlFieldWrite, all_writes: list[ControlFieldWrite]) -> bool:
    """Does this write restore a prior violation on the same issue/field?

    The guard's signature: a later entry whose ``from`` equals the
    violation's ``to`` and whose ``to`` equals the violation's ``from`` (the
    original value put back), by a different author. A second violation
    (A→B then B→C) does not match — its ``to`` differs from the original.
    """
    for prior in all_writes:
        if prior is w or prior.issue_key != w.issue_key or prior.field != w.field:
            continue
        prior_at = _parse_jira_ts(prior.at)
        w_at = _parse_jira_ts(w.at)
        if prior_at is None or w_at is None or w_at <= prior_at:
            continue
        if w.author == prior.author:
            continue
        if w.from_value == prior.to_value and w.to_value == prior.from_value:
            return True
    return False


def _classify_reverted(writes: list[ControlFieldWrite], issue_key: str) -> None:
    """Mark violations a later changelog entry restored (the guard's revert)."""
    mine = [w for w in writes if w.issue_key == issue_key]
    for w in mine:
        if any(_is_revert_entry(r, writes) and r.from_value == w.to_value and r.to_value == w.from_value for r in mine):
            w.reverted = True
