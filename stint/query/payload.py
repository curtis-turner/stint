"""Construct Jira `{"fields": ...}` payloads from Pydantic model instances.

Per-field-type rules:
  - Text / TextArea / URL / ReadOnly: bare string (Cloud wraps ``description`` in ADF)
  - Select / RadioButtons: ``{"value": "S1"}``
  - MultiSelect / Checkboxes: ``[{"value": "S1"}, ...]``
  - Labels:        bare list of strings, no ``value`` wrapping: ``["a", "b"]``
  - User:          ``{"accountId": "..."}`` on Jira Cloud; ``name`` is the
                   legacy fallback
  - MultiUser:     list of the same shape
  - Version:       ``{"name": "1.0"}``
  - MultiVersion:  ``[{"name": "1.0"}, ...]``
  - Group:         ``{"name": "jira-admins"}``
  - MultiGroup:    ``[{"name": "jira-admins"}, ...]``
  - Number:        bare number
  - Date:          ``"YYYY-MM-DD"``
  - DateTime:      ``"YYYY-MM-DDTHH:MM:SS+00:00"``
  - System fields (summary, reporter, assignee, priority, description): per-attr

The function emits ``fields`` only for attributes the caller asked for via
``only=`` (used on UPDATE for dirty fields). For INSERT, the caller passes
``only=None`` to include everything that isn't ``key``. Unset optional
fields are skipped only on INSERT; on UPDATE (``only=`` given) an explicit
None is emitted so Jira clears the field.

Insert-specific system fields (project, issuetype) are added by
``build_insert_payload`` based on the model class. ``reporter`` is stripped
from inserts (Jira sets it from the caller identity; see the insert builder).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from stint.exceptions import ConfigurationError
from stint.fields import (
    CheckboxesField,
    DateField,
    DateTimeField,
    GroupField,
    LabelsField,
    MultiGroupField,
    MultiSelectField,
    MultiUserField,
    MultiVersionField,
    NumberField,
    RadioButtonsField,
    ReadOnlyField,
    SelectField,
    TextAreaField,
    TextField,
    URLField,
    UserField,
    VersionField,
    _FieldType,
)
from stint.query.adf import wrap_plain_text
from stint.registry import registry
from stint.state.file import SimpleMapping

if TYPE_CHECKING:
    from pydantic import BaseModel

    from stint.fields import CustomField
    from stint.state.file import StateFile


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
    ``key``. Use ``only={...}`` on update with the dirty set; the update path
    also admits explicit ``None`` values (null clears the field in Jira)
    instead of skipping them like the insert path does.
    """
    model = type(instance)
    payload: dict[str, Any] = {}
    for attr_name, field_info in model.model_fields.items():
        if attr_name == "key":
            continue
        if only is not None and attr_name not in only:
            continue
        value = getattr(instance, attr_name, None)
        if value is None and not field_info.is_required() and only is None:
            # INSERT path: skip unset optionals to avoid clearing existing
            # Jira data. On UPDATE (`only` is the caller's dirty set) a None
            # is a deliberate clear — emit null, which Jira accepts for most
            # fields.
            continue
        cf = _custom_field_meta(field_info)
        if cf is None:
            _raise_on_shadows_declared_custom_field(model, attr_name, field_info)
            key, val = _emit_system_field(attr_name, value, is_cloud=is_cloud)
            payload[key] = val
            continue
        mapping = state.custom_fields.get(cf.alias)
        if mapping is None:
            raise ConfigurationError(
                f"build_fields_payload: custom-field alias {cf.alias!r} "
                f"is not in state. Run `stint stamp` or `stint upgrade` first."
            )
        payload[mapping.id] = _emit_custom_field(cf.type, value, is_cloud=is_cloud)
    return payload


def _raise_on_shadows_declared_custom_field(model: type[BaseModel], attr_name: str, field_info: Any) -> None:
    """Refuse a payload key that silently shadows a declared CustomField.

    The failure mode (live tenant, 2026-11): a model attribute named like an
    existing CustomField's declaration but WITHOUT ``Annotated[T, cf]`` is
    emitted as a literal system-field key (``test_text_field``) instead of
    resolving to ``customfield_10321`` — Jira answers "Field 'test_text_field'
    cannot be set ... or unknown" and the real bug stays invisible.

    The collision matches on the declaration's *name* and *alias*, compared
    after stripping separators (``TestTextField`` ↔ ``test_text_field``).
    Real system fields are unaffected: they carry no matching declaration.

    There is a second, subtler form of the same failure: the attribute *is*
    Annotated, but with a name that is also the module-level CustomField
    variable's name. Under ``from __future__ import annotations`` the
    annotation string is resolved against the class namespace, where that name
    now holds the attribute's default (``None``), so the metadata is ``[None]``
    and the declaration is lost. Renaming the *variable* (not the attribute)
    fixes it; say so explicitly rather than blaming a missing annotation.
    """

    def _norm(value: str) -> str:
        return value.replace("_", "").replace("-", "").lower()

    shadowed_by_default = any(item is None for item in field_info.metadata)
    target = _norm(attr_name)
    for alias, declared in registry.custom_fields.items():
        if target in (_norm(declared.name), _norm(alias), _norm(alias + "_field")):
            if shadowed_by_default:
                raise ConfigurationError(
                    f"payload builder: attribute {attr_name!r} on {model.__name__!r} "
                    f"is annotated with a name that resolves to None instead of the "
                    f"CustomField {alias!r} ({declared.name!r}): the annotation's "
                    f"second argument has the same name as the attribute itself, so "
                    f"Python finds the attribute's default (None) first. Rename the "
                    f"CustomField *variable* (e.g. {attr_name}_cf or a _field suffix) "
                    f"and annotate with that, leaving the attribute name unchanged."
                )
            raise ConfigurationError(
                f"payload builder: attribute {attr_name!r} on {model.__name__!r} "
                f"matches declared CustomField {alias!r} ({declared.name!r}) but is "
                f"not Annotated with it, so it would be sent to Jira as a literal "
                f"{attr_name!r} field key (Jira rejects it as unknown) instead of "
                f"resolving through the state file. Annotate the attribute: "
                f"{attr_name}: Annotated[<type>, <the CustomField declared as "
                f"{declared.name!r}>] = None"
            )


def build_insert_payload(
    instance: BaseModel,
    state: StateFile,
    *,
    is_cloud: bool,
    project_key: str,
) -> dict[str, Any]:
    """Build the full POST /issue body. Pulls project + issuetype ids from state.

    ``reporter`` is never sent on inserts: Jira Cloud takes the reporter from
    the caller's identity at creation and rejects an explicit one ("Field
    'reporter' cannot be set. It is not on the appropriate screen, or
    unknown") even when the field is on the create screen. The model keeps
    the attribute — hydration fills it (as an accountId) when issues are
    read back, and UPDATE payloads (PUT) accept real reporter changes.
    """
    model = type(instance)
    fields = build_fields_payload(instance, state, is_cloud=is_cloud)
    fields.pop("reporter", None)

    project_mapping = state.projects.get(project_key)
    if project_mapping is None:
        raise ConfigurationError(
            f"build_insert_payload: project {project_key!r} is not in state. "
            f"Run `stint stamp` or `stint upgrade` first."
        )
    fields["project"] = {"key": project_mapping.key}

    issuetype_alias = getattr(model, "__alias__", None)
    if not issuetype_alias:
        raise ConfigurationError(f"build_insert_payload: model {model.__name__!r} has no __alias__")
    it_mapping = None
    # Prefer project-scoped ID for TMP projects
    if project_key in state.tmp_projects:
        for alias_key, wt_id in state.tmp_projects[project_key].worktypes.items():
            if alias_key.lower() == issuetype_alias.lower():
                it_mapping = SimpleMapping(id=wt_id)
                break
    if it_mapping is None:
        it_mapping = next((state.issuetypes[k] for k in state.issuetypes if k.lower() == issuetype_alias.lower()), None)
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
    from stint.fields import CustomField

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
    if value is None:
        # Null clears the field server-side; never wrap None in a shape
        # ({"value": "None"}, {"accountId": "None"}, ...).
        return None
    if field_type in (SelectField, RadioButtonsField):
        return {"value": str(value)}
    if field_type in (MultiSelectField, CheckboxesField):
        if isinstance(value, (list, tuple)):
            return [{"value": str(v)} for v in value]
        return [{"value": str(value)}]
    if field_type is LabelsField:
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [str(value)]
    if field_type is UserField:
        return {"accountId": str(value)} if is_cloud else {"name": str(value)}
    if field_type is MultiUserField:
        values = value if isinstance(value, (list, tuple)) else [value]
        key = "accountId" if is_cloud else "name"
        return [{key: str(v)} for v in values]
    if field_type is VersionField:
        return {"name": str(value)}
    if field_type is MultiVersionField:
        if isinstance(value, (list, tuple)):
            return [{"name": str(v)} for v in value]
        return [{"name": str(value)}]
    if field_type is GroupField:
        return {"name": str(value)}
    if field_type is MultiGroupField:
        if isinstance(value, (list, tuple)):
            return [{"name": str(v)} for v in value]
        return [{"name": str(value)}]
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
    if field_type in (TextField, TextAreaField, URLField, ReadOnlyField):
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
      - None values pass through as null (deliberate clear on update).
      - description on Cloud: wrap in ADF.
      - reporter/assignee: {"accountId":...} on Cloud; {"name":...} is the
        legacy fallback.
      - priority: {"name": "High"}.
    """
    if value is None:
        return attr_name, None
    if attr_name == "description" and is_cloud:
        return attr_name, wrap_plain_text(str(value))
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
