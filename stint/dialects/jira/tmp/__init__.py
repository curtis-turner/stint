"""Team-managed-project (TMP) support.

Everything under this package talks to Atlassian's undocumented, unsupported
internal APIs (see ``tmp_spike_conclusion.md`` and ``tmp_dialect_design.md``
at the repo root for the full design record). Nothing outside this package
may import from inside it, except the dialect registry once TMP is wired up
as a selectable, opt-in dialect -- a later phase. A company-managed user's
process must never load this code.

Build status: incremental across several phases (see ``tmp_dialect_design.md``
Build phases). ``TmpDialect`` does not yet satisfy ``BaseDialect`` -- it has no
``detect``/``reflect``/project CRUD/data-plane methods yet -- so it must not be
registered as a selectable dialect until later phases complete it.
"""

from stint.dialects.jira.tmp.dialect import TmpDialect

__all__ = ["TmpDialect"]
