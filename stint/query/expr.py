"""Expression AST for query construction.

A ``Column`` represents a queryable attribute on an IssueType. Comparing it
to a value (or wrapping it in helper functions) produces an ``Expression``,
which knows how to compile itself to JQL given a state file (to resolve
custom-field aliases to ids).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stint.state.file import StateFile


# ── Quote helpers ────────────────────────────────────────────────────
def _quote(value: Any) -> str:
    """Render a Python value as a JQL literal."""
    if value is None:
        return "EMPTY"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quote_list(values: list[Any]) -> str:
    return "(" + ", ".join(_quote(v) for v in values) + ")"


# ── Column ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Column:
    """One attribute of an IssueType, usable in a query.

    `attr_name` is the Pydantic attribute name (e.g. ``"severity"``).
    `cf_alias` is set when this column is linked to a CustomField, ``None``
    for Jira system fields. The JQL field name is resolved at compile time
    against the state file.
    """

    model: type
    attr_name: str
    cf_alias: str | None = None  # None → Jira system field name

    def resolve_jql_field(self, state: StateFile) -> str:
        if self.cf_alias is None:
            return self.attr_name
        mapping = state.custom_fields.get(self.cf_alias)
        if mapping is None:
            raise KeyError(
                f"Column {self.model.__name__}.{self.attr_name} references "
                f"custom-field alias {self.cf_alias!r}, which is not in the "
                f"state file. Run `stint stamp` or `stint upgrade` first."
            )
        # JQL accepts both forms; the ``cf[N]`` notation is more portable
        # across Jira versions.
        if mapping.id.startswith("customfield_"):
            num = mapping.id[len("customfield_") :]
            return f"cf[{num}]"
        return mapping.id

    # Comparison operators produce Expression nodes
    def __eq__(self, other: Any) -> Expression:  # ty: ignore[invalid-method-override]
        return _Cmp(self, "=", other)

    def __ne__(self, other: Any) -> Expression:  # ty: ignore[invalid-method-override]
        return _Cmp(self, "!=", other)

    def __lt__(self, other: Any) -> Expression:
        return _Cmp(self, "<", other)

    def __le__(self, other: Any) -> Expression:
        return _Cmp(self, "<=", other)

    def __gt__(self, other: Any) -> Expression:
        return _Cmp(self, ">", other)

    def __ge__(self, other: Any) -> Expression:
        return _Cmp(self, ">=", other)

    def in_(self, values: list[Any]) -> Expression:
        return _In(self, list(values))

    def not_in(self, values: list[Any]) -> Expression:
        return _NotIn(self, list(values))

    def contains(self, substring: str) -> Expression:
        """JQL `~` operator: full-text contains. Only valid on text fields."""
        return _Cmp(self, "~", substring)

    def is_null(self) -> Expression:
        return _Cmp(self, "is", None)

    def is_not_null(self) -> Expression:
        return _Cmp(self, "is not", None)

    def __hash__(self) -> int:
        return hash((self.model, self.attr_name))


# ── Expression nodes ─────────────────────────────────────────────────
class Expression:
    """Base. Subclasses implement compile()."""

    def compile(self, state: StateFile) -> str:
        raise NotImplementedError

    def __and__(self, other: Expression) -> Expression:
        return _And([self, other])

    def __or__(self, other: Expression) -> Expression:
        return _Or([self, other])

    def __invert__(self) -> Expression:
        return _Not(self)


@dataclass
class _Cmp(Expression):
    column: Column
    op: str
    value: Any

    def compile(self, state: StateFile) -> str:
        field = self.column.resolve_jql_field(state)
        if self.op in ("is", "is not"):
            return f"{field} {self.op} EMPTY"
        return f"{field} {self.op} {_quote(self.value)}"


@dataclass
class _In(Expression):
    column: Column
    values: list[Any]

    def compile(self, state: StateFile) -> str:
        field = self.column.resolve_jql_field(state)
        return f"{field} in {_quote_list(self.values)}"


@dataclass
class _NotIn(Expression):
    column: Column
    values: list[Any]

    def compile(self, state: StateFile) -> str:
        field = self.column.resolve_jql_field(state)
        return f"{field} not in {_quote_list(self.values)}"


@dataclass
class _And(Expression):
    parts: list[Expression]

    def compile(self, state: StateFile) -> str:
        return "(" + " AND ".join(p.compile(state) for p in self.parts) + ")"


@dataclass
class _Or(Expression):
    parts: list[Expression]

    def compile(self, state: StateFile) -> str:
        return "(" + " OR ".join(p.compile(state) for p in self.parts) + ")"


@dataclass
class _Not(Expression):
    inner: Expression

    def compile(self, state: StateFile) -> str:
        return f"NOT ({self.inner.compile(state)})"


# ── Public combinators ───────────────────────────────────────────────
def and_(*exprs: Expression) -> Expression:
    if not exprs:
        raise ValueError("and_() requires at least one expression")
    if len(exprs) == 1:
        return exprs[0]
    return _And(list(exprs))


def or_(*exprs: Expression) -> Expression:
    if not exprs:
        raise ValueError("or_() requires at least one expression")
    if len(exprs) == 1:
        return exprs[0]
    return _Or(list(exprs))


def not_(expr: Expression) -> Expression:
    return _Not(expr)
