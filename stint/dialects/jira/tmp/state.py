"""In-memory alias -> Jira-id mappings for one team-managed project.

Deliberately not ``stint.state.file.StateFile``: that file has no home for a
``layoutId`` (nothing in CMP's incremental-write model needs "the id of the
thing this list of items belongs to"), and wiring TMP into the real,
persisted state file means resolving how ``Engine``/CLI select a TMP dialect
at all -- an open question explicitly deferred past this phase (see
``tmp_dialect_design.md``). ``TmpState`` is in-memory only for now; a caller
that wants persistence serializes/deserializes it itself (it's a plain
dataclass of ``dict[str, str]``, so this is a trivial YAML/JSON round-trip
whenever that wiring happens).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TmpState:
    """Alias -> Jira id, for each kind of object this dialect manages."""

    fields: dict[str, str] = field(default_factory=dict)  # field alias -> fieldId
    worktypes: dict[str, str] = field(default_factory=dict)  # worktype alias -> issuetype id
    layout_ids: dict[str, str] = field(default_factory=dict)  # worktype alias -> layoutId
