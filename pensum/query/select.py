"""``select(Model).where(...)`` query construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pensum.query.expr import Expression, _And

if TYPE_CHECKING:
    from pensum.state.file import StateFile


@dataclass
class Select:
    """Buildable query. Compose with ``.where()``, run via ``session.scalars()``."""

    model: type
    filters: list[Expression] = field(default_factory=list)
    limit_n: int | None = None
    order_by_attrs: list[tuple[str, str]] = field(default_factory=list)
    # [(jql_field_name, "ASC"|"DESC")] populated by order_by()

    def where(self, *exprs: Expression) -> Select:
        """Add AND-combined filters."""
        self.filters.extend(exprs)
        return self

    def limit(self, n: int) -> Select:
        self.limit_n = int(n)
        return self

    def order_by(self, column, direction: str = "ASC") -> Select:
        """Sort. Direction is `ASC` or `DESC`. Multiple calls append."""
        from pensum.query.expr import Column

        if not isinstance(column, Column):
            raise TypeError(f"order_by expects a Column (e.g. Bug.c.created), got {type(column).__name__}")
        direction = direction.upper()
        if direction not in ("ASC", "DESC"):
            raise ValueError(f"order_by direction must be ASC or DESC, got {direction!r}")
        # Stash the column itself; resolution happens at compile time.
        self.order_by_attrs.append((column, direction))  # type: ignore[arg-type]
        return self

    def compile(self, state: StateFile) -> str:
        """Render the JQL string. Empty filters → empty JQL (matches everything)."""
        jql_parts: list[str] = []
        if self.filters:
            if len(self.filters) == 1:
                jql_parts.append(self.filters[0].compile(state))
            else:
                jql_parts.append(_And(list(self.filters)).compile(state))
        if self.order_by_attrs:
            obs = []
            for col, direction in self.order_by_attrs:
                obs.append(f"{col.resolve_jql_field(state)} {direction}")  # type: ignore[union-attr]
            jql_parts.append("ORDER BY " + ", ".join(obs))
        return " ".join(jql_parts)


def select(model: type) -> Select:
    """Begin a query against `model`."""
    return Select(model=model)
