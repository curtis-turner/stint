"""Schema-plane declarations for issue security schemes.

``SecurityScheme``/``SecurityLevel``/``SecurityLevelMember`` are declaration
dataclasses registered with the ``Registry`` when they are constructed inside
user schema code. They are distinct from the reflection snapshots in
``stint.state.snapshot`` (same field shape, different lifecycle: declarations
register themselves; snapshots are built per reflect run and never touch the
registry — a dataclass ``__post_init__`` on the snapshot type would pollute
the registry from inside ``parse_issue_security_scheme``).

Fields mirror the reflected snapshots one-to-one so declarations read like
the reflection payload (see ``examples/company_managed/security_scheme.py``):
``SecurityLevel.id``/``name`` double as the member-facing designation used in
``SecurityScheme.default_level``, and ``SecurityLevelMember.name`` carries
the group name or user email/accountId (Jira's ``holder.parameter``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stint.exceptions import RegistryError
from stint.registry import registry


@dataclass
class SecurityLevelMember:
    """One holder of a security level: a user or a group.

    ``name`` is the *parameter* Jira resolves: a group name (``type="group"``)
    or an email / raw accountId (``type="user"``). Emails are resolved to
    accountIds at apply time via the user-search API. ``id`` is an optional
    declared designation only — Jira assigns its own member ids.
    """

    id: str = ""
    type: str = "group"  # "user" or "group"
    name: str = ""  # group name, user email, or raw accountId

    def __post_init__(self) -> None:
        # dataclasses-generated __init__ calls this; _declaring_module walks
        # past the dataclasses module frame and attributes the declaration to
        # the user module (M5 pattern). Levels only need the member values;
        # members do not register individually.
        self.name = self.name.strip()
        if self.type not in ("user", "group"):
            raise RegistryError(
                f"SecurityLevelMember {self.name!r}: unknown type {self.type!r}; expected 'user' or 'group'"
            )


@dataclass
class SecurityLevel:
    """A security level inside a ``SecurityScheme``.

    ``id`` is the designation used by ``SecurityScheme.default_level`` and by
    ops; the Jira-assigned level id is recorded in the state file at apply
    time. ``name`` must be unique within the scheme (Jira enforces it).
    """

    id: str = ""  # designation; alias
    name: str = ""  # Jira level name
    description: str = ""
    is_default: bool = False
    members: list[SecurityLevelMember] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.name and not self.id:
            self.id = self.name  # undeclared designations default to the name


@dataclass
class SecurityScheme:
    """An issue security scheme plus its levels, bound to a ``Project``.

    Registering fields:

    - ``alias``: stable identifier across tenants (state-file key).
    - ``levels``: ordered ``SecurityLevel`` declarations; ``is_default=True``
      and ``default_level=`` are two spellings of the same choice — both work.
    - ``project``: optional project key hint. The *authoritative* linkage is
      ``Project.__security_scheme__``; a ``project`` value that no declared
      Project (by key) references emits a revision diff warning.

    Auto-generated write ops: create/delete scheme with levels, update
    header, add level, add member, associate to project. Level deletion and
    member removal destructive calls are not yet auto-managed (see diff).
    """

    alias: str
    name: str = ""
    description: str = ""
    levels: list[SecurityLevel] = field(default_factory=list)
    default_level: str | None = None  # a level's id or name
    project: str | None = None  # project key hint (non-authoritative)

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("SecurityScheme declaration requires a non-empty alias")
        seen: set[str] = set()
        for level in self.levels:
            designation = level.id or level.name
            if not level.name:
                raise RegistryError(f"SecurityScheme {self.alias!r}: a SecurityLevel has no name")
            if designation in seen:
                raise RegistryError(
                    f"SecurityScheme {self.alias!r}: duplicate level designation {designation!r} "
                    f"(id and name must uniquely identify a level within the scheme)"
                )
            seen.add(designation)
        if self.default_level is not None and not _resolve_level_designation(self.default_level, self.levels):
            raise RegistryError(
                f"SecurityScheme {self.alias!r}: default_level {self.default_level!r} matches no "
                f"SecurityLevel (looked for a level id or name among: "
                f"{sorted((lv.id or lv.name) for lv in self.levels)}; comparisons are case-insensitive)"
            )
        registry.register_security_scheme(self)

    @property
    def default_level_designation(self) -> str | None:
        """Canonical default-level designation (the level's ``id``), or inline
        ``is_default=True`` honored first."""
        for level in self.levels:
            if level.is_default:
                return level.id
        if self.default_level:
            resolved = _resolve_level_designation(self.default_level, self.levels)
            return resolved.id if resolved else None
        return None


def _resolve_level_designation(ref: str, levels: list[Any]) -> Any:
    """Find a declared level by id or name, exact first, case-insensitive fallback."""
    for level in levels:
        if level.id and level.id == ref:
            return level
        if level.name == ref:
            return level
    lowered = ref.lower()
    for level in levels:
        if (level.id.lower() if level.id else "") == lowered:
            return level
        if level.name.lower() == lowered:
            return level
    return None
