"""Return shapes for the TMP fields-gateway, layout, and reflect operations.

Kept inside the TMP module, not ``stint.state.snapshot``: TMP is project-local
and scheme-less, so most of its shapes don't fit the CMP scheme-derived
Snapshot classes. ``TmpSnapshot`` is the one exception -- it wraps a genuine
``Snapshot`` (reused as-is, imported one-directionally from core; the
isolation rule is that core must never import from here, not the reverse)
alongside the per-work-type layout data ``Snapshot`` has no room for, since
CMP expresses field/issuetype association via screens and schemes, which TMP
simply doesn't have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stint.state.snapshot import Snapshot


@dataclass(frozen=True)
class TmpFieldOption:
    """One option on a TMP select-style field.

    ``option_id`` is ``None`` for an option that does not exist yet -- on a
    write, this means "create it"; the API assigns the id and returns it.
    """

    value: str
    option_id: str | None = None
    external_uuid: str | None = None
    color: str | None = None  # bare GraphQL enum literal, e.g. "ORANGE_DARKER"


@dataclass(frozen=True)
class TmpField:
    """A TMP project-scoped custom field, as returned by create/edit."""

    field_id: str
    name: str
    description: str
    scope: str  # "PROJECT" (project-scoped) or "GLOBAL" (shared with CMP)
    options: tuple[TmpFieldOption, ...] = ()


@dataclass(frozen=True)
class TmpFieldAssociation:
    """One row of the field-enumeration read: a field visible on the project."""

    field_id: str
    name: str
    scope: str
    type_key: str
    options: tuple[TmpFieldOption, ...] = ()


@dataclass(frozen=True)
class TmpWorkType:
    """A TMP project-scoped work type (issue type), as returned by create.

    There is no separate update endpoint: renaming/re-describing a work type
    happens through ``write_layout``'s owner data (see ``tmp_crud_surface.md``
    -- the work-type edit screen IS the issue-layout screen).
    """

    id: str
    name: str
    avatar_id: str
    hierarchy_level: int = 0


@dataclass(frozen=True)
class TmpLayoutOwner:
    """The work type (issue type) that owns a layout."""

    id: str
    name: str
    description: str
    avatar_id: str
    icon_url: str


@dataclass(frozen=True)
class TmpLayoutItem:
    """One field on a work-type's issue layout: its position and config."""

    field_id: str
    key: str
    name: str
    type_key: str
    custom: bool
    global_: bool
    required: bool
    section: str  # "primary" | "secondary" | "content"
    position: int
    external_uuid: str
    description: str
    operations: dict[str, object]
    provider: dict[str, str]


@dataclass(frozen=True)
class TmpLayout:
    """A work type's full issue layout: the layoutId, its owner, and fields."""

    layout_id: str
    owner: TmpLayoutOwner
    items: tuple[TmpLayoutItem, ...] = ()


@dataclass(frozen=True)
class TmpProjectContext:
    """Resolved identifiers for one team-managed project.

    ``project_id`` (numeric) is what the layout read/write and work-type
    endpoints key on; ``project_uuid`` is what work-type *creation* needs
    instead (a separate identifier space, confirmed live). Resolving both
    together means callers only need a project key.
    """

    cloud_id: str
    project_id: str
    project_uuid: str
    key: str
    name: str


@dataclass(frozen=True)
class TmpSnapshot:
    """TMP's reflected state for one project.

    ``snapshot`` is a genuine, dialect-agnostic ``Snapshot`` (server_info,
    custom_fields, issuetypes, projects populated; screens/schemes left at
    their empty defaults, since TMP has none). ``layouts`` carries the one
    thing ``Snapshot`` has no room for: which fields are on which work type,
    in what order, and whether they're required -- keyed by issuetype id.
    """

    snapshot: Snapshot
    layouts: dict[str, TmpLayout] = field(default_factory=dict)


@dataclass(frozen=True)
class TmpCapabilityCheck:
    """One read-only pre-flight check's result."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class TmpCapabilityReport:
    """The result of ``TmpDialect.check_capabilities``.

    Covers only read-only surfaces (auth, the fields gateway, the gira layout
    reader) -- it cannot confirm the mutating surfaces' ``@optIn`` keys are
    still correct, since the only way to prove that is to attempt an actual
    mutation. See ``check_capabilities``'s docstring for the full reasoning.
    """

    checks: tuple[TmpCapabilityCheck, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)
