"""``Bug.c.severity`` accessor returning queryable Column objects.

The IssueType metaclass attaches one of these to each subclass at definition
time. Looking up ``cls.c.<attr>`` returns a fresh ``Column`` bound to the
class + attribute. Lookups for unknown attributes raise AttributeError with
a list of known fields, which makes typos easy to debug.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pensum.fields import CustomField
from pensum.query.expr import Column

if TYPE_CHECKING:
    from pydantic import BaseModel


class Columns:
    """Namespace exposing each Pydantic field of an IssueType as a Column."""

    def __init__(self, model: type[BaseModel]) -> None:
        self._model = model
        self._cf_aliases: dict[str, str] = {}
        for attr_name, field_info in model.model_fields.items():
            cf = next(
                (m for m in field_info.metadata if isinstance(m, CustomField)),
                None,
            )
            if cf is not None:
                self._cf_aliases[attr_name] = cf.alias

    def __getattr__(self, name: str) -> Column:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._model.model_fields:
            known = sorted(self._model.model_fields)
            raise AttributeError(f"{self._model.__name__} has no attribute {name!r}. Known fields: {known}")
        return Column(
            model=self._model,
            attr_name=name,
            cf_alias=self._cf_aliases.get(name),
        )

    def __dir__(self) -> list[str]:
        return sorted(self._model.model_fields)
