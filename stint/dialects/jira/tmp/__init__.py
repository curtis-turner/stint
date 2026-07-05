"""Team-managed-project (TMP) support.

Everything under this package talks to Atlassian's undocumented, unsupported
internal APIs (see ``tmp_spike_conclusion.md`` and ``tmp_dialect_design.md``
at the repo root for the full design record). Nothing outside this package
imports from inside it except ``stint.engine`` -- and even there only
lazily, inside ``create_tmp_engine``, so a company-managed user's process
never loads this code just by doing ``import stint`` or calling
``create_engine``.

Build status: all six design-doc phases plus core wiring are done (see
``tmp_dialect_design.md``). ``TmpDialect`` still does not satisfy
``BaseDialect`` (no ``detect``; ``reflect`` takes a required ``project_key``),
so it is never in ``create_engine``'s dialect registry -- it has its own,
separate ``stint.engine.create_tmp_engine`` / ``TmpEngine`` entry point
instead.
"""

from stint.dialects.jira.tmp.dialect import TmpDialect

__all__ = ["TmpDialect"]
