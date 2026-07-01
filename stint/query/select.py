"""``select(Model).where(...)`` query construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from stint.query.expr import Column, Expression, _And

if TYPE_CHECKING:
    from stint.schema.issuetype import IssueType
    from stint.state.file import StateFile

M = TypeVar("M", bound="IssueType")


@dataclass
class Select(Generic[M]):
    """Buildable query. Compose with ``.where()``, run via ``session.scalars()``."""

    model: type[M]
    filters: list[Expression] = field(default_factory=list)
    limit_n: int | None = None
    order_by_attrs: list[tuple[Column, str]] = field(default_factory=list)
    # [(column, "ASC"|"DESC")] populated by order_by(); resolved to JQL at compile()

    def where(self, *exprs: Expression) -> Select[M]:
        """Add AND-combined filters."""
        self.filters.extend(exprs)
        return self

    def limit(self, n: int) -> Select[M]:
        self.limit_n = int(n)
        return self

    def order_by(self, column: Column, direction: str = "ASC") -> Select[M]:
        """Sort. Direction is `ASC` or `DESC`. Multiple calls append."""
        if not isinstance(column, Column):
            raise TypeError(f"order_by expects a Column (e.g. Bug.c.created), got {type(column).__name__}")
        direction = direction.upper()
        if direction not in ("ASC", "DESC"):
            raise ValueError(f"order_by direction must be ASC or DESC, got {direction!r}")
        # Stash the column itself; resolution happens at compile time.
        self.order_by_attrs.append((column, direction))
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
                obs.append(f"{col.resolve_jql_field(state)} {direction}")
            jql_parts.append("ORDER BY " + ", ".join(obs))
        return " ".join(jql_parts)


def select(model: type[M]) -> Select[M]:
    """Begin a query against `model`."""
    return Select(model=model)
