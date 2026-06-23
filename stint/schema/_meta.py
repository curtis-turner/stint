"""Shared metaclass for IssueType and Project.

Pulls stint-specific dunders out of the class namespace before Pydantic's
ModelMetaclass sees them, then reattaches them as plain class attributes.
This keeps Pydantic from misinterpreting them as model fields.

Per-base validation and registration live in subclasses of StintMeta defined
alongside IssueType and Project. Doing validation in __init_subclass__ would
fire before the dunders are reattached.
"""

from __future__ import annotations

from typing import Any

from pydantic._internal._model_construction import ModelMetaclass

STINT_DUNDERS = frozenset(
    {
        # IssueType
        "__alias__",
        "__title__",
        "__description__",
        "__screen_scheme__",
        "__field_configuration__",
        # Project
        "__key__",
        "__lead__",
        "__style__",
        "__issuetypes__",
    }
)


class StintMeta(ModelMetaclass):
    """Metaclass that lets stint-specific dunders coexist with Pydantic fields."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        stint_attrs = {k: namespace.pop(k) for k in list(namespace) if k in STINT_DUNDERS}
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
        for k, v in stint_attrs.items():
            setattr(cls, k, v)
        return cls
