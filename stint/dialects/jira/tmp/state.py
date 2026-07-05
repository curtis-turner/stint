"""In-memory alias -> Jira-id mappings for one team-managed project.

Persists via ``stint.state.file.StateFile.tmp_projects`` (keyed by the
project's alias): that field holds ``TmpProjectState``, a plain-dict record
shaped identically to this class but defined in core so ``stint/state/file.py``
never has to import from here (the isolation rule in ``tmp_dialect_design.md``
is one-directional -- this module may import core, core must never import
this). ``to_project_state``/``from_project_state`` below are the round-trip
between the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stint.state.file import TmpProjectState


@dataclass
class TmpState:
    """Alias -> Jira id, for each kind of object this dialect manages."""

    fields: dict[str, str] = field(default_factory=dict)  # field alias -> fieldId
    worktypes: dict[str, str] = field(default_factory=dict)  # worktype alias -> issuetype id
    layout_ids: dict[str, str] = field(default_factory=dict)  # worktype alias -> layoutId


def to_project_state(state: TmpState) -> TmpProjectState:
    """Convert to the persistable shape for ``StateFile.tmp_projects[alias]``."""
    return TmpProjectState(
        fields=dict(state.fields),
        worktypes=dict(state.worktypes),
        layout_ids=dict(state.layout_ids),
    )


def from_project_state(mapping: TmpProjectState) -> TmpState:
    """Load a ``TmpState`` back out of a ``StateFile.tmp_projects[alias]`` entry."""
    return TmpState(
        fields=dict(mapping.fields),
        worktypes=dict(mapping.worktypes),
        layout_ids=dict(mapping.layout_ids),
    )
