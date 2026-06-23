"""Migration autogeneration.

Public API:
  - autogenerate(schema_module, state, snapshot, *, message, allow_delete=False)
      -> str  (Python source of the new migration file)
  - render_empty(message, *, parents) -> str  (skeleton with no ops)
  - render_merge(message, *, parents) -> str  (merge-only skeleton)
  - new_revision_id() -> str
  - new_filename(message, when=None) -> str
  - stamp(schema_module, state, snapshot) -> StampReport
"""

from stint.autogen.emit import (
    new_filename,
    new_revision_id,
    render_empty,
    render_merge,
)

__all__ = [
    "new_filename",
    "new_revision_id",
    "render_empty",
    "render_merge",
]
