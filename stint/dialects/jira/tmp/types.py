"""Jira custom-field type vocabulary accepted by the TMP write surface.

The TMP write path (``createCustomFieldInProjectAndAddToAllIssueTypes``)
accepts only a subset of the Jira custom-field type ids that the classic
company-managed REST API supports. Out of the 18 ``_FieldType`` classes in
``stint.fields``, only 9 currently work on TMP. The full live-tested
breakdown lives in ``ISSUE_DRAFT.md`` (TMP-B, Finding 1); this module is
the runtime surface for that fact.

Two consumers:
- ``build_tmp_desired`` calls ``tmp_type_key_for(cf.type)`` while walking
  a schema, so an unsupported type raises ``TmpFieldTypeError`` (a
  ``ConfigurationError`` subclass) at plan time -- failing loudly during
  ``stint apply`` is strictly better than discovering the same gap on a
  live ``400 Invalid field type specified`` from the internal mutation.
- ``tools/tmp_type_probe.py`` is the live discovery tool: it tries every
  ``_FieldType`` against the create mutation on a disposable TMP project,
  so a future Atlassian type-vocabulary change can be re-confirmed in one
  run and the mapping updated here in lockstep.
"""

from __future__ import annotations

from typing import Final

from stint.exceptions import ConfigurationError
from stint.fields import (
    CheckboxesField,
    DateField,
    DateTimeField,
    LabelsField,
    NumberField,
    SelectField,
    TextAreaField,
    TextField,
    URLField,
)

TMP_TYPE_KEY_PREFIX: Final[str] = "com.atlassian.jira.plugin.system.customfieldtypes:"
"""Prefix Jira uses for every system custom-field type id in this vocabulary.

A live-confirmed invariant, kept here so a single search-replace catches it
if Atlassian ever renames the plugin module.
"""


# Mapping from stint's ``_FieldType`` class to the Jira type id the TMP
# ``createCustomFieldInProjectAndAddToAllIssueTypes`` mutation accepts for it.
# Confirmed live 2026-07-05 (see ``ISSUE_DRAFT.md`` TMP-B); 9 of 18 types work,
# the rest fail with ``400 Invalid field type specified``. Re-run
# ``tools/tmp_type_probe.py`` to re-verify against a live tenant; update this
# map in lockstep with what the probe reports.
TMP_FIELD_TYPE_KEYS: Final[dict[type, str]] = {
    TextField: f"{TMP_TYPE_KEY_PREFIX}textfield",
    TextAreaField: f"{TMP_TYPE_KEY_PREFIX}textarea",
    SelectField: f"{TMP_TYPE_KEY_PREFIX}select",
    CheckboxesField: f"{TMP_TYPE_KEY_PREFIX}multicheckboxes",
    LabelsField: f"{TMP_TYPE_KEY_PREFIX}labels",
    URLField: f"{TMP_TYPE_KEY_PREFIX}url",
    NumberField: f"{TMP_TYPE_KEY_PREFIX}float",
    DateField: f"{TMP_TYPE_KEY_PREFIX}datepicker",
    DateTimeField: f"{TMP_TYPE_KEY_PREFIX}datetime",
}


class TmpFieldTypeError(ConfigurationError):
    """A ``CustomField`` declared a type the TMP write surface does not support.

    Subclasses ``ConfigurationError`` so the existing ``stint apply`` error
    path renders it cleanly (same as any other schema-misconfiguration
    failure -- the user fixes the schema, not the wire).
    """


def tmp_type_key_for(field_type: type) -> str:
    """Resolve a stint ``_FieldType`` to the Jira type id the TMP mutation
    accepts, or raise ``TmpFieldTypeError`` eagerly.

    Eager matters: surfacing the unsupported-type case during
    ``build_tmp_desired`` (plan time) instead of from a live 400 at write
    time means the user sees a precise message naming the offending field
    alias and the supported set, with no audit-log noise on a real
    tenant.
    """
    key = TMP_FIELD_TYPE_KEYS.get(field_type)
    if key is not None:
        return key
    supported = ", ".join(sorted(t.__name__ for t in TMP_FIELD_TYPE_KEYS))
    raise TmpFieldTypeError(
        f"CustomField type {field_type.__name__!r} is not supported by the TMP "
        f"write surface. Supported types: {supported}. "
        f"Either pick a supported type or remove the field from the TMP schema."
    )
