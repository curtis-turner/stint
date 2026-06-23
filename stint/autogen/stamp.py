"""Brownfield: bring existing Jira objects under stint management.

Reads schema declarations (registry) and a reflected snapshot, matches each
declared alias to an existing Jira object by display name (or `key` for
projects), and populates the state file with the matched id mappings.

Performs NO writes to Jira. The result is a state file that subsequent
`stint revision --autogenerate` calls can diff against.

Matching strategy:
  - CustomField:        match by name
  - IssueType:          match by name
  - Screen:             match by name (populates tab_ids from snapshot)
  - ScreenScheme:       match by name
  - FieldConfiguration: match by name
  - Project:            match by key (more reliable than name)
  - ITSS / FCS:         match by the synthesized name from desired snapshot
                        (e.g. "PLAT Issue Type Screen Scheme")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stint.autogen.desired import build_desired_snapshot
from stint.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    ScreenMapping,
    SimpleMapping,
)

if TYPE_CHECKING:
    from stint.registry import Registry
    from stint.state.file import StateFile
    from stint.state.snapshot import Snapshot


@dataclass
class StampReport:
    matched: list[tuple[str, str, str]] = field(default_factory=list)
    # (object_type, alias, jira_id)
    unmatched: list[tuple[str, str]] = field(default_factory=list)
    # (object_type, alias)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    # (object_type, alias, reason) — already in state with different id, etc.


def stamp(
    state: StateFile,
    snapshot: Snapshot,
    *,
    registry: Registry | None = None,
) -> StampReport:
    """Mutate `state` in place to absorb existing Jira objects that match
    aliases declared in the registry. Returns a report of what happened.
    """
    desired = build_desired_snapshot(registry)
    report = StampReport()

    _stamp_custom_fields(state, snapshot, desired, report)
    _stamp_issuetypes(state, snapshot, desired, report)
    _stamp_screens(state, snapshot, desired, report)
    _stamp_screen_schemes(state, snapshot, desired, report)
    _stamp_field_configurations(state, snapshot, desired, report)
    _stamp_itss(state, snapshot, desired, report)
    _stamp_fcs(state, snapshot, desired, report)
    _stamp_projects(state, snapshot, desired, report)
    return report


# ── Custom fields ────────────────────────────────────────────────────
def _stamp_custom_fields(state, snapshot, desired, report) -> None:
    by_name = {cf.name: cf for cf in snapshot.custom_fields.values()}
    for alias, want in desired.custom_fields.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("custom_field", alias))
            continue
        if alias in state.custom_fields:
            existing = state.custom_fields[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "custom_field",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        options = dict(match.options)
        state.custom_fields[alias] = CustomFieldMapping(id=match.id, options=options)
        report.matched.append(("custom_field", alias, match.id))


# ── Issue types ──────────────────────────────────────────────────────
def _stamp_issuetypes(state, snapshot, desired, report) -> None:
    by_name = {it.name: it for it in snapshot.issuetypes.values()}
    for alias, want in desired.issuetypes.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("issuetype", alias))
            continue
        if alias in state.issuetypes:
            existing = state.issuetypes[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "issuetype",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        state.issuetypes[alias] = SimpleMapping(id=match.id)
        report.matched.append(("issuetype", alias, match.id))


# ── Screens ──────────────────────────────────────────────────────────
def _stamp_screens(state, snapshot, desired, report) -> None:
    by_name = {s.name: s for s in snapshot.screens.values()}
    for alias, want in desired.screens.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("screen", alias))
            continue
        if alias in state.screens:
            existing = state.screens[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "screen",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        tab_ids = {tab.name: tab.id for tab in match.tabs}
        state.screens[alias] = ScreenMapping(id=match.id, tab_ids=tab_ids)
        report.matched.append(("screen", alias, match.id))


# ── Screen schemes ───────────────────────────────────────────────────
def _stamp_screen_schemes(state, snapshot, desired, report) -> None:
    by_name = {s.name: s for s in snapshot.screen_schemes.values()}
    for alias, want in desired.screen_schemes.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("screen_scheme", alias))
            continue
        if alias in state.screen_schemes:
            existing = state.screen_schemes[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "screen_scheme",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        state.screen_schemes[alias] = SimpleMapping(id=match.id)
        report.matched.append(("screen_scheme", alias, match.id))


# ── Field configurations ─────────────────────────────────────────────
def _stamp_field_configurations(state, snapshot, desired, report) -> None:
    by_name = {fc.name: fc for fc in snapshot.field_configurations.values()}
    for alias, want in desired.field_configurations.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("field_configuration", alias))
            continue
        if alias in state.field_configurations:
            existing = state.field_configurations[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "field_configuration",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        state.field_configurations[alias] = SimpleMapping(id=match.id)
        report.matched.append(("field_configuration", alias, match.id))


# ── ITSS ─────────────────────────────────────────────────────────────
def _stamp_itss(state, snapshot, desired, report) -> None:
    by_name = {s.name: s for s in snapshot.issuetype_screen_schemes.values()}
    for alias, want in desired.issuetype_screen_schemes.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("issuetype_screen_scheme", alias))
            continue
        if alias in state.issuetype_screen_schemes:
            existing = state.issuetype_screen_schemes[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "issuetype_screen_scheme",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        state.issuetype_screen_schemes[alias] = SimpleMapping(id=match.id)
        report.matched.append(("issuetype_screen_scheme", alias, match.id))


# ── FCS ──────────────────────────────────────────────────────────────
def _stamp_fcs(state, snapshot, desired, report) -> None:
    by_name = {s.name: s for s in snapshot.field_configuration_schemes.values()}
    for alias, want in desired.field_configuration_schemes.items():
        match = by_name.get(want.name)
        if match is None:
            report.unmatched.append(("field_configuration_scheme", alias))
            continue
        if alias in state.field_configuration_schemes:
            existing = state.field_configuration_schemes[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "field_configuration_scheme",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
            continue
        state.field_configuration_schemes[alias] = SimpleMapping(id=match.id)
        report.matched.append(("field_configuration_scheme", alias, match.id))


# ── Projects (matched by key) ────────────────────────────────────────
def _stamp_projects(state, snapshot, desired, report) -> None:
    by_key = {p.key: p for p in snapshot.projects.values()}
    for alias, want in desired.projects.items():
        match = by_key.get(want.key)
        if match is None:
            report.unmatched.append(("project", alias))
            continue
        # ProjectSnapshot.style is "classic" (CMP) on DC, "classic" (CMP) or
        # "next-gen" (TMP) on Cloud. Normalize to stint's vocabulary.
        style = "team-managed" if match.style == "next-gen" else "company-managed"
        if alias in state.projects:
            existing = state.projects[alias]
            if existing.id != match.id:
                report.skipped.append(
                    (
                        "project",
                        alias,
                        f"state already maps to {existing.id!r}, Jira has {match.id!r}",
                    )
                )
                continue
            # Same id — backfill style/key if missing or wrong. Pre-M7 state
            # files default style to "company-managed" without observing Jira.
            if existing.style != style or existing.key != match.key:
                state.projects[alias] = ProjectMapping(
                    id=existing.id,
                    style=style,
                    key=match.key,
                )
            continue
        state.projects[alias] = ProjectMapping(
            id=match.id,
            style=style,
            key=match.key,
        )
        report.matched.append(("project", alias, match.id))
