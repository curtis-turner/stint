"""IssueType base. Dual role: schema declaration + Pydantic-validated data container."""

from __future__ import annotations

import typing
from types import NoneType, UnionType
from typing import Any, cast

from pydantic import BaseModel

from stint.exceptions import ConfigurationError
from stint.fields import CustomField, MultiSelectField, SelectField
from stint.registry import registry
from stint.schema._meta import StintMeta

if typing.TYPE_CHECKING:
    from stint.query.columns import Columns

_SELECT_FIELD_TYPES: tuple[type, ...] = (SelectField, MultiSelectField)


class IssueTypeMeta(StintMeta):
    """Validates and registers IssueType subclasses after dunders are reattached."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> type[IssueType]:
        cls = cast("type[IssueType]", super().__new__(mcs, name, bases, namespace, **kwargs))
        if name == "IssueType":
            return cls
        if not getattr(cls, "__alias__", None):
            raise ConfigurationError(f"IssueType {name!r} is missing __alias__")
        if getattr(cls, "__title__", None) is None:
            cls.__title__ = name
        cls.__custom_field_map__ = _extract_and_validate_custom_fields(cls)
        # Attach a `c` accessor so users can build queries with class-level
        # field references (e.g. ``Bug.c.severity == "S1"``). Imported lazily
        # to avoid a cycle (query/ depends on schema/).
        from stint.query.columns import Columns

        cls.c = Columns(cls)
        registry.register_issuetype(cls)
        return cls


def _extract_and_validate_custom_fields(cls: type[BaseModel]) -> dict[str, CustomField]:
    """Walk Pydantic field metadata and pick out CustomField instances from Annotated.

    For select-style CustomFields, also verifies the Pydantic annotation is a
    Literal (or list[Literal] for multiselect) whose values exactly match the
    CustomField's options. Raises ConfigurationError on mismatch so option-list
    drift is caught at class-definition time, not at apply time.

    Also catches the common shadowing trap: when the module-level CustomField
    has the same Python name as the class attribute, Pydantic's metadata
    processor sees an unresolvable ForwardRef instead of the CustomField.
    Detect that case and explain the fix.

    Returns a map of Python attribute name -> CustomField. Attributes without
    a CustomField in their Annotated metadata are treated as system fields by
    the dialect and do not appear here.
    """
    from typing import ForwardRef

    result: dict[str, CustomField] = {}
    for attr_name, field_info in cls.model_fields.items():
        for meta in field_info.metadata:
            if isinstance(meta, ForwardRef) and meta.__forward_arg__ == attr_name:
                raise ConfigurationError(
                    f"{cls.__name__}.{attr_name}: the Annotated metadata "
                    f"references the name {attr_name!r}, which collides with "
                    f"the class attribute. Pydantic resolves it as an "
                    f"unresolvable ForwardRef instead of the intended "
                    f"CustomField. Rename the module-level CustomField using "
                    f"a distinct name (convention: `{attr_name}_field` or "
                    f"`{attr_name}_cf`) and reference that in Annotated."
                )
        cf = next((m for m in field_info.metadata if isinstance(m, CustomField)), None)
        if cf is None:
            continue
        if cf.type in _SELECT_FIELD_TYPES:
            _validate_select_annotation(cls.__name__, attr_name, field_info.annotation, cf)
        result[attr_name] = cf
    return result


def _validate_select_annotation(
    class_name: str,
    attr_name: str,
    annotation: Any,
    cf: CustomField,
) -> None:
    """Raise ConfigurationError if the annotation's Literal values don't match cf.options."""
    expected = set(cf.options)
    actual = _extract_literal_values(annotation)
    if actual is None:
        raise ConfigurationError(
            f"{class_name}.{attr_name} is linked to select-style CustomField "
            f"{cf.alias!r} with options {sorted(expected)}, but the Pydantic "
            f"annotation is not a Literal (or list[Literal] for MultiSelect). "
            f"Wrap the type so Pydantic validates the option values: "
            f"Annotated[Literal[{', '.join(repr(o) for o in cf.options)}], {cf.alias}_field]"
        )
    if actual != expected:
        only_in_annotation = sorted(actual - expected)
        only_in_options = sorted(expected - actual)
        detail = []
        if only_in_annotation:
            detail.append(f"in annotation but not in options: {only_in_annotation}")
        if only_in_options:
            detail.append(f"in options but not in annotation: {only_in_options}")
        raise ConfigurationError(
            f"{class_name}.{attr_name}: Pydantic Literal values do not match "
            f"CustomField {cf.alias!r} options. " + "; ".join(detail) + ". "
            "Keep them in sync or remove one source of truth."
        )


def _extract_literal_values(annotation: Any) -> set[Any] | None:
    """Find the inner Literal[...] values of an annotation. None if no Literal present.

    Handles:
      - Literal["a","b"]                              -> {"a","b"}
      - Literal["a","b"] | None                       -> {"a","b"}
      - Optional[Literal["a","b"]]                    -> {"a","b"}
      - list[Literal["a","b"]]                        -> {"a","b"}    (for MultiSelect)
      - list[Literal["a","b"]] | None                 -> {"a","b"}
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Literal:
        return set(args)

    if origin in (typing.Union, UnionType):
        non_none = [a for a in args if a is not NoneType]
        if len(non_none) == 1:
            return _extract_literal_values(non_none[0])
        return None

    if origin in (list, typing.List):  # noqa: UP006
        if len(args) == 1:
            return _extract_literal_values(args[0])
        return None

    return None


class IssueType(BaseModel, metaclass=IssueTypeMeta):
    """Base class for all issuetypes.

    Subclasses declare:

    - ``__alias__``: stable cross-environment identifier (required).
    - ``__title__``: Jira display name. Optional. Defaults to the class name.
    - ``__description__``: optional Jira description.
    - ``__screen_scheme__``: optional ScreenScheme reference (CMP / DC only).
    - ``__field_configuration__``: optional FieldConfiguration reference (CMP / DC only).

    Pydantic-typed attributes declare the data-plane fields. The same class is
    used both as a schema-plane object (declaring what issuetype to provision
    in Jira) and as a data-plane container (Pydantic validates
    ``Bug(severity="S5")`` and rejects it without any network call).

    To link a Pydantic attribute to a custom field, use ``Annotated``:

        severity = CustomField(alias="bug_severity", name="Severity",
                               type=SelectField, options=["S1","S2","S3","S4"])

        class Bug(IssueType):
            __alias__ = "bug"
            severity: Annotated[Literal["S1","S2","S3","S4"], severity]

    Attributes without a CustomField in their Annotated metadata are treated
    as Jira system fields (``summary``, ``description``, ``reporter``, etc.).
    """

    # Jira's issue key (e.g. "PROJ-123"). None until the issue is created;
    # set on hydrate (reads) and after insert (writes). Skipped by payload
    # builders and dirty tracking — Jira assigns it, never the client.
    key: str | None = None

    # Set by IssueTypeMeta after class creation. Declared so the type checker
    # sees them; the metaclass populates the values.
    __title__: typing.ClassVar[str]
    __custom_field_map__: typing.ClassVar[dict[str, CustomField]]
    c: typing.ClassVar[Columns]
    # Project keys that include this issuetype; appended by ProjectMeta.
    __projects__: typing.ClassVar[tuple[str, ...]] = ()
