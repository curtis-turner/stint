"""Schema-plane declarations for workflows (``Workflow``/``Status``/
``Transition``) and workflow schemes (``WorkflowScheme``).

Declaration dataclasses registered with the ``Registry`` when constructed
inside user schema code (same lifecycle as ``PermissionScheme``). A project
binds one with ``Project.__workflow_scheme__``.

Jira model, in stint's vocabulary:

- ``Status``: a tenant-global named status with a category
  (``new``/``indeterminate``/``done``). Referenced by name across workflows.
- ``Transition``: one workflow transition. ``from_`` lists the statuses it
  is available from, ``to`` is the target. ``restrict_to`` narrows who may
  use it — a project role, group, user, or permission key. Unrestricted
  transitions require only the permission scheme's ``TRANSITION_ISSUES``.
- ``Workflow``: statuses + transitions + the initial status. Undeclared
  properties (validators, postfunctions, hidden/system transitions on an
  adopted workflow) are left unmanaged: adoption never rewrites them.
- ``WorkflowScheme``: which workflow is the project default and any
  per-issuetype overrides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stint.exceptions import RegistryError
from stint.registry import registry
from stint.schema.permissions import ProjectRole  # noqa: F401  (re-exported restrict holder)
from stint.schema.roles import Group, User

__all__ = [
    "Permission",
    "ProjectRole",
    "Status",
    "Transition",
    "TransitionRestrictHolder",
    "Workflow",
    "WorkflowScheme",
]

# Category names Jira accepts on status create/update; ids for the built-in
# categories are 1/2/3 → new/indeterminate/done.
STATUS_CATEGORIES: frozenset[str] = frozenset({"new", "indeterminate", "done"})


@dataclass(frozen=True)
class Permission:
    """Transition restrict holder: anyone holding the named permission key
    (e.g. ``Permission("TRANSITION_ISSUES")``)."""

    key: str

    def __post_init__(self) -> None:
        if not self.key or not self.key.strip():
            raise ValueError("Permission requires a non-empty permission key")


TransitionRestrictHolder = ProjectRole | Group | User | Permission


@dataclass
class Status:
    """A tenant-global status. ``name`` is the reference used by workflows
    (Jira creates or reuses it by name); ``category`` is one of
    ``new``/``indeterminate``/``done``; ``description`` is optional."""

    name: str
    category: str = "indeterminate"
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise RegistryError("Status declaration requires a non-empty name")
        if self.category not in STATUS_CATEGORIES:
            raise RegistryError(
                f"Status {self.name!r}: unknown category {self.category!r}; expected one of {sorted(STATUS_CATEGORIES)}"
            )


@dataclass
class Transition:
    """One workflow transition.

    ``from_`` lists the statuses the transition is available from (Status
    instances or names); ``to`` is the target status. ``restrict_to`` maps
    to the platform restrict rule — every holder listed must be satisfied
    (project role, group, user, or permission key). Empty means unrestricted.
    """

    name: str
    from_: list[Status | str] = field(default_factory=list)
    to: Status | str = ""
    restrict_to: list[TransitionRestrictHolder] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise RegistryError("Transition declaration requires a non-empty name")


@dataclass
class Workflow:
    """A workflow: statuses, transitions, and the initial status.

    Registering fields:

    - ``alias``: stable identifier across tenants (state-file key).
    - ``name``: Jira display name; must be unique per tenant; defaults to
      the alias.
    - ``statuses``: at least one; ``initial`` must be one of them.
    - ``transitions``: each ``from_``/``to`` must reference a declared
      status (Jira adds the implicit Create transition into ``initial``).

    Validators and postfunctions are not declarable: adopting an existing
    workflow leaves them untouched, and created workflows carry none beyond
    Jira's defaults.
    """

    alias: str
    name: str = ""
    description: str = ""
    statuses: list[Status] = field(default_factory=list)
    initial: Status | None = None
    transitions: list[Transition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("Workflow declaration requires a non-empty alias")
        if not self.name:
            self.name = self.alias
        if not self.statuses:
            raise RegistryError(f"Workflow {self.alias!r}: at least one status is required")
        if self.initial is None:
            raise RegistryError(f"Workflow {self.alias!r}: initial status is required")
        names = [s.name for s in self.statuses]
        if self.initial.name not in names:
            raise RegistryError(
                f"Workflow {self.alias!r}: initial status {self.initial.name!r} is not one of the declared statuses"
            )
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise RegistryError(f"Workflow {self.alias!r}: duplicate status names {dupes}")
        status_by_name = {s.name: s for s in self.statuses}
        transition_names: list[str] = []
        for t in self.transitions:
            transition_names.append(t.name)
            for frm in t.from_:
                if (frm.name if isinstance(frm, Status) else frm) not in status_by_name:
                    raise RegistryError(
                        f"Workflow {self.alias!r} transition {t.name!r}: source status "
                        f"{frm.name if isinstance(frm, Status) else frm!r} is not declared"
                    )
            target = t.to.name if isinstance(t.to, Status) else t.to
            if not target:
                raise RegistryError(f"Workflow {self.alias!r} transition {t.name!r}: target status is required")
            if target not in status_by_name:
                raise RegistryError(
                    f"Workflow {self.alias!r} transition {t.name!r}: target status {target!r} is not declared"
                )
        if len(set(transition_names)) != len(transition_names):
            dupes = sorted({n for n in transition_names if transition_names.count(n) > 1})
            raise RegistryError(f"Workflow {self.alias!r}: duplicate transition names {dupes}")
        registry.register_workflow(self)


@dataclass
class WorkflowScheme:
    """A workflow scheme: the default workflow plus per-issuetype overrides.

    The binding is ``Project.__workflow_scheme__`` (authoritative);
    ``project`` is an optional project key hint like other scheme types.
    ``mappings`` keys are issuetype *aliases* (as declared on the project's
    ``__issuetypes__``); values are ``Workflow`` instances.
    """

    alias: str
    name: str = ""
    description: str = ""
    default: Workflow | None = None
    mappings: dict[str, Workflow] = field(default_factory=dict)
    project: str | None = None  # project key hint (non-authoritative)

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("WorkflowScheme declaration requires a non-empty alias")
        if not self.name:
            self.name = self.alias
        if self.default is None:
            raise RegistryError(f"WorkflowScheme {self.alias!r}: default workflow is required")
        for it_alias, workflow in self.mappings.items():
            if not isinstance(workflow, Workflow):
                raise RegistryError(
                    f"WorkflowScheme {self.alias!r}: mapping for issuetype {it_alias!r} "
                    f"must be a Workflow, got {type(workflow).__name__}"
                )
        registry.register_workflow_scheme(self)
