"""Construct Jira `{"fields": ...}` payloads from Pydantic model instances.

Per-field-type rules:
  - Text / TextArea: bare string (Cloud wraps ``description`` in ADF)
  - Select:        ``{"value": "S1"}``
  - MultiSelect:   ``[{"value": "S1"}, ...]``
  - User:          ``{"name": "..."}`` (DC) or ``{"accountId": "..."}`` (Cloud)
  - Number:        bare number
  - Date:          ``"YYYY-MM-DD"``
  - DateTime:      ``"YYYY-MM-DDTHH:MM:SS+00:00"``
  - System fields (summary, reporter, assignee, priority, description): per-attr

The function emits ``fields`` only for attributes the caller asked for via
``only=`` (used on UPDATE for dirty fields). For INSERT, the caller passes
``only=None`` to include everything that isn't ``key``.

Insert-specific system fields (project, issuetype) are added by
``build_insert_payload`` based on the model class.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from pensum.exceptions import ConfigurationError
from pensum.fields import (
    DateField,
    DateTimeField,
    MultiSelectField,
    NumberField,
    SelectField,
    TextAreaField,
    TextField,
    UserField,
    _FieldType,
)
from pensum.query.adf import wrap_plain_text

if TYPE_CHECKING:
    from pydantic import BaseModel

    from pensum.fields import CustomField
    from pensum.state.file import StateFile


# Cloud system text fields that need ADF wrapping. ``description`` is the
# main one; comments and worklog bodies also, but those aren't issue fields.
_CLOUD_ADF_SYSTEM_FIELDS = {"description"}


def build_fields_payload(
    instance: BaseModel,
    state: StateFile,
    *,
    is_cloud: bool,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Return the ``fields`` mapping. Caller wraps as ``{"fields": ...}``.

    ``only``: attribute names to include. ``None`` means everything except
    ``key``. Use ``only={...}`` on update with the dirty set.
    """
    model = type(instance)
    payload: dict[str, Any] = {}
    for attr_name, field_info in model.model_fields.items():
        if attr_name == "key":
            continue
        if only is not None and attr_name not in only:
            continue
        value = getattr(instance, attr_name, None)
        if value is None and not field_info.is_required():
            # Skip unset optionals to avoid clearing existing Jira data.
            continue
        cf = _custom_field_meta(field_info)
        if cf is None:
            key, val = _emit_system_field(attr_name, value, is_cloud=is_cloud)
            payload[key] = val
            continue
        mapping = state.custom_fields.get(cf.alias)
        if mapping is None:
            raise ConfigurationError(
                f"build_fields_payload: custom-field alias {cf.alias!r} "
                f"is not in state. Run `pensum stamp` or `pensum upgrade` first."
            )
        payload[mapping.id] = _emit_custom_field(cf.type, value, is_cloud=is_cloud)
    return payload


def build_insert_payload(
    instance: BaseModel,
    state: StateFile,
    *,
    is_cloud: bool,
    project_key: str,
) -> dict[str, Any]:
    """Build the full POST /issue body. Pulls project + issuetype ids from state."""
    model = type(instance)
    fields = build_fields_payload(instance, state, is_cloud=is_cloud)

    project_mapping = state.projects.get(project_key)
    if project_mapping is None:
        raise ConfigurationError(
            f"build_insert_payload: project {project_key!r} is not in state. "
            f"Run `pensum stamp` or `pensum upgrade` first."
        )
    fields["project"] = {"id": project_mapping.id}

    issuetype_alias = getattr(model, "__alias__", None)
    if not issuetype_alias:
        raise ConfigurationError(f"build_insert_payload: model {model.__name__!r} has no __alias__")
    it_mapping = state.issuetypes.get(issuetype_alias)
    if it_mapping is None:
        raise ConfigurationError(f"build_insert_payload: issuetype {issuetype_alias!r} is not in state.")
    fields["issuetype"] = {"id": it_mapping.id}

    return {"fields": fields}


def build_update_payload(
    instance: BaseModel,
    state: StateFile,
    *,
    is_cloud: bool,
    dirty: set[str],
) -> dict[str, Any]:
    """Build a PUT /issue/{key} body with only the dirty fields."""
    return {
        "fields": build_fields_payload(
            instance,
            state,
            is_cloud=is_cloud,
            only=dirty,
        )
    }


# ── Internal: per-type emission ──────────────────────────────────────
def _custom_field_meta(field_info: Any) -> CustomField | None:
    from pensum.fields import CustomField

    return next(
        (m for m in field_info.metadata if isinstance(m, CustomField)),
        None,
    )


def _emit_custom_field(
    field_type: type[_FieldType],
    value: Any,
    *,
    is_cloud: bool,
) -> Any:
    if field_type is SelectField:
        return {"value": str(value)}
    if field_type is MultiSelectField:
        if isinstance(value, (list, tuple)):
            return [{"value": str(v)} for v in value]
        return [{"value": str(value)}]
    if field_type is UserField:
        return {"accountId": str(value)} if is_cloud else {"name": str(value)}
    if field_type is DateField:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)
    if field_type is DateTimeField:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)
    if field_type is NumberField:
        return value
    if field_type in (TextField, TextAreaField):
        return value
    # Unknown field type → pass through and hope Jira understands
    return value


def _emit_system_field(
    attr_name: str,
    value: Any,
    *,
    is_cloud: bool,
) -> tuple[str, Any]:
    """System field translation. Returns (Jira-field-name, JSON-shaped value).

    Most system fields share the attribute name (summary, description, priority,
    reporter, assignee). A few need transformation:
      - description on Cloud: wrap in ADF.
      - reporter/assignee: {"name":...} (DC) / {"accountId":...} (Cloud).
      - priority: {"name": "High"}.
    """
    if attr_name == "description" and is_cloud:
        return attr_name, wrap_plain_text(str(value) if value is not None else "")
    if attr_name in ("reporter", "assignee", "creator"):
        if is_cloud:
            return attr_name, {"accountId": str(value)}
        return attr_name, {"name": str(value)}
    if attr_name == "priority":
        return attr_name, {"name": str(value)}
    if isinstance(value, datetime):
        return attr_name, value.isoformat()
    if isinstance(value, date):
        return attr_name, value.isoformat()
    return attr_name, value
