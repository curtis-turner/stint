"""``stint stamp`` for team-managed projects: fill ``StateFile.tmp_projects``.

The CMP stamp algorithm (``stint.autogen.stamp``) matches against CMP
surfaces -- global issue types, screens, schemes -- and synthesizes CMP
scheme objects a team-managed project cannot have. Nothing it writes is
read by ``stint apply``, whose only TMP inputs are
``state_file.tmp_projects[key].fields`` and ``.worktypes``.

This module matches the same declarations against what the TMP dialect can
actually reflect (project-scoped fields, project-scoped work types):

- fields: by display name **and** type id (same discriminator as the
  apply-time adoption scan), so a GLOBAL field sharing a declared field's
  name is never stamped
- work types: by display name, among the project's own work types

No writes to Jira. Existing mappings are preserved: a match that disagrees
with state is reported as ``skipped``, not overwritten -- same semantics as
the CMP stamp.
"""

from __future__ import annotations

from stint.autogen.stamp import StampReport
from stint.state.file import ProjectMapping, StateFile, TmpProjectState
from stint.state.snapshot import CustomFieldSnapshot, IssueTypeSnapshot

from .desired import TmpDesired
from .models import TmpSnapshot


def _match_by_name(
    kind: str,
    alias: str,
    candidates: list[tuple[str, str]],
    existing_id: str | None,
    report: StampReport,
) -> str | None:
    """Resolve one declared alias against (id, discriminator) candidates.

    Returns the matched id, or ``None`` after recording the outcome:
    ``unmatched`` when nothing qualifies, ``skipped`` with the candidate ids
    when the name is ambiguous (refusing to guess, L20) or with a conflict
    reason when state already maps the alias elsewhere.
    """
    if len(candidates) > 1:
        ids = ", ".join(sorted(cid for cid, _ in candidates))
        report.skipped.append((kind, alias, f"matches multiple Jira objects ({ids}); refusing to guess"))
        return None
    if not candidates:
        report.unmatched.append((kind, alias))
        return None
    match_id = candidates[0][0]
    if existing_id is not None:
        if existing_id != match_id:
            report.skipped.append((kind, alias, f"state already maps to {existing_id!r}, Jira has {match_id!r}"))
        return None
    return match_id


def stamp_tmp(state: StateFile, project_key: str, desired: TmpDesired, snapshot: TmpSnapshot) -> StampReport:
    """Mutate ``state`` in place: populate ``tmp_projects[project_key]`` from
    a reflected ``TmpSnapshot`` and the schema's TMP declarations. Returns a
    report of matched/unmatched/skipped aliases."""
    report = StampReport()
    core = snapshot.snapshot
    existing = state.tmp_projects.get(project_key)
    fields = dict(existing.fields) if existing else {}
    worktypes = dict(existing.worktypes) if existing else {}
    layout_ids = dict(existing.layout_ids) if existing else {}

    # ── Fields: by name and type ─────────────────────────────────────
    by_name: dict[str, list[CustomFieldSnapshot]] = {}
    for field in core.custom_fields.values():
        by_name.setdefault(field.name, []).append(field)
    for alias, want in desired.fields.items():
        candidates = [(f.id, f.type_id) for f in by_name.get(want.name, []) if f.type_id == want.type_key]
        match_id = _match_by_name("custom_field", alias, candidates, fields.get(alias), report)
        if match_id is not None:
            fields[alias] = match_id
            report.matched.append(("custom_field", alias, match_id))

    # ── Work types: by name, among this project's own types ──────────
    by_wt_name: dict[str, list[IssueTypeSnapshot]] = {}
    for wt in core.issuetypes.values():
        by_wt_name.setdefault(wt.name, []).append(wt)
    for alias, want in desired.worktypes.items():
        candidates = [(wt.id, wt.name) for wt in by_wt_name.get(want.name, [])]
        match_id = _match_by_name("worktype", alias, candidates, worktypes.get(alias), report)
        if match_id is not None:
            worktypes[alias] = match_id
            layout = snapshot.layouts.get(match_id)
            if layout is not None:
                layout_ids[alias] = layout.layout_id
            report.matched.append(("worktype", alias, match_id))

    state.tmp_projects[project_key] = TmpProjectState(fields=fields, worktypes=worktypes, layout_ids=layout_ids)

    # Mirror apply's final save: the CMP-style project mapping stays in sync
    # so the state file reads coherently from either path.
    project = core.projects.get(project_key)
    if project is not None and project_key not in state.projects:
        state.projects[project_key] = ProjectMapping(id=project.id, style="team-managed", key=project.key)
        report.matched.append(("project", project_key, project.id))
    return report
