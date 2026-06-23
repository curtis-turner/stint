"""Hydrate a Jira issue JSON payload into a Pydantic model instance.

The session needs to map each Pydantic attribute back to its slot in
``issue["fields"]``. For CustomField-linked attributes, the slot key is the
``customfield_X`` id resolved via the state file. For system fields, the
slot key is the attribute name itself (``summary``, ``description``, etc.).

The shape of each value also varies:
  - select / multiselect: ``{"value": "S1", "id": "100"}`` → extract ``.value``
  - reporter / assignee: ``{"name": "jdoe"}`` (DC) or ``{"accountId": "..."}`` (Cloud)
  - priority: ``{"name": "High"}``
  - text fields: bare string

For 0.1, hydration handles the common cases. Datetime coercion and ADF
parsing are M6 territory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stint.fields import (
    MultiSelectField,
    SelectField,
    UserField,
    _FieldType,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from stint.fields import CustomField
    from stint.state.file import StateFile


def field_keys_for_model(model: type[BaseModel], state: StateFile) -> list[str]:
    """Compute the Jira field keys to request when fetching this model.

    For each Pydantic attribute, returns either the Jira system field name
    or the resolved ``customfield_X`` id. Skips the ``key`` attribute (it
    comes from issue["key"], not issue["fields"]).
    """
    keys: list[str] = []
    for attr_name, field_info in model.model_fields.items():
        if attr_name == "key":
            continue
        cf = _custom_field_meta(field_info)
        if cf is None:
            keys.append(attr_name)
            continue
        mapping = state.custom_fields.get(cf.alias)
        if mapping is None:
            # Field not yet stamped/upgraded into state. Skip — the resulting
            # instance will have the attribute set to its default.
            continue
        keys.append(mapping.id)
    return keys


def hydrate(
    model: type[BaseModel],
    issue: dict[str, Any],
    state: StateFile,
) -> BaseModel:
    """Construct a model instance from a Jira issue payload.

    Unknown fields and missing-in-payload fields fall back to the Pydantic
    default. The instance is fully validated by Pydantic on construction.
    """
    fields_data = issue.get("fields", {}) or {}
    kwargs: dict[str, Any] = {}
    if "key" in model.model_fields:
        kwargs["key"] = issue.get("key")
    for attr_name, field_info in model.model_fields.items():
        if attr_name == "key":
            continue
        cf = _custom_field_meta(field_info)
        if cf is None:
            raw = fields_data.get(attr_name)
            if raw is None and field_info.is_required():
                continue  # let Pydantic raise if truly required
            kwargs[attr_name] = _coerce_system_field(attr_name, raw)
            continue

        mapping = state.custom_fields.get(cf.alias)
        if mapping is None:
            continue
        raw = fields_data.get(mapping.id)
        if raw is None:
            continue
        kwargs[attr_name] = _coerce_custom_field(cf.type, raw)
    return model(**kwargs)


def _custom_field_meta(field_info: Any) -> CustomField | None:
    from stint.fields import CustomField

    return next(
        (m for m in field_info.metadata if isinstance(m, CustomField)),
        None,
    )


# ── Coercion helpers ─────────────────────────────────────────────────
def _coerce_system_field(attr_name: str, raw: Any) -> Any:
    """Most system fields are objects with a ``name`` or ``displayName`` key.
    Strings and primitives pass through unchanged."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        # Common cases:
        #   reporter/assignee/creator: {"name": "...", "accountId": "..."}
        #   priority/status/resolution: {"name": "..."}
        #   issuetype: {"name": "..."}
        if "accountId" in raw:
            return raw["accountId"]
        if "name" in raw:
            return raw["name"]
        if "value" in raw:
            return raw["value"]
        return raw
    return raw


def _coerce_custom_field(field_type: type[_FieldType], raw: Any) -> Any:
    """Custom-field shapes:
    - SelectField:      {"value": "S1", "id": "100"}
    - MultiSelectField: [{"value": "S1"}, {"value": "S2"}]
    - UserField:        {"name": "..."} or {"accountId": "..."}
    - others (text, number, date, datetime): bare scalar
    """
    if raw is None:
        return None
    if field_type is SelectField:
        if isinstance(raw, dict):
            return raw.get("value")
        return raw
    if field_type is MultiSelectField:
        if isinstance(raw, list):
            return [v.get("value") if isinstance(v, dict) else v for v in raw]
        return raw
    if field_type is UserField:
        if isinstance(raw, dict):
            return raw.get("accountId") or raw.get("name")
        return raw
    return raw
