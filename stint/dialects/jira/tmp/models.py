"""Return shapes for the TMP fields-gateway and layout-read operations.

Kept inside the TMP module, not ``stint.state.snapshot``: TMP is project-local
and scheme-less, so its shapes don't fit the CMP scheme-derived Snapshot
classes. A future TMP reflect (build phase 4) will translate these into the
dialect-agnostic ``Snapshot`` the planner consumes.
"""

from __future__ import annotations

from dataclasses import dataclass


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
