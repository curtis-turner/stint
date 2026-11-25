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
    (or display name when the payload omits it).
    """

    issue_key: str
    field: str
    author: str
    at: str  # Jira's ISO timestamp from the changelog entry
    from_value: str | None
    to_value: str | None


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

    def raise_for_writes(self) -> None:
        if not self.writes:
            return
        details = "\n".join(
            f"  - {w.at} {w.issue_key}: {w.field} by {w.author} ({w.from_value!r} -> {w.to_value!r})"
            for w in self.writes
        )
        raise StintError(f"out-of-band writes to control fields on project {self.project!r}:\n{details}")


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


async def audit_control_fields(
    engine: Engine,
    project_cls: type[Project],
    state: StateFile,
    *,
    since: datetime | str,
    allowed_writers: list[str] | tuple[str, ...] = (),
) -> ControlFieldAuditReport:
    """Scan recent changelog entries for writes to declared control fields.

    ``since`` bounds the scan: a datetime (formatted into the JQL
    ``updated >= "..."`` filter, and compared against each changelog entry's
    timestamp) or a raw JQL-ready string. ``allowed_writers`` lists emails or
    accountIds whose writes are expected (the service account, automation
    users); anything else touching a control field lands in the report.

    Read-only: one JQL search plus changelog GETs per updated issue.
    """
    dialect = engine.dialect
    control_by_issuetype = declared_control_fields(project_cls)
    flat = tuple(sorted({name for names in control_by_issuetype.values() for name in names}))
    report = ControlFieldAuditReport(
        project=project_cls.__key__,
        since=_resolve_since(since),
        control_fields=flat,
        allowed_writers=tuple(allowed_writers),
    )
    if not flat:
        return report

    # Writers: emails resolve to accountIds; raw values also match display
    # names so operators can allow "svc-automation" without knowing ids.
    allowed_ids: set[str] = set()
    for value in allowed_writers:
        if "@" in value:
            allowed_ids.add(await dialect.resolve_user(value))
        else:
            allowed_ids.add(value)

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
            if author_id in allowed_ids:
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
    return report
