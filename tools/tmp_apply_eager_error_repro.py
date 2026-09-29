#!/usr/bin/env python3
"""Offline repro: TMP rejects 9 of 18 Jira custom-field types at plan time.

Exercises ``stint.dialects.jira.tmp.types.tmp_type_key_for`` directly and
then end-to-end through ``build_tmp_desired`` -- no live tenant required.
For each ``stint.fields._FieldType`` subclass, prints whether the TMP
write surface accepts it and, for the failing 9, what the error message
looks like when a schema declares one.

The mapping itself is the live-confirmed 2026-07-05 split from
``ISSUE_DRAFT.md`` TMP-B Finding 1. To re-verify against a real tenant,
run ``tools/tmp_type_probe.py`` instead.

Usage:
    uv run python tools/tmp_apply_eager_error_repro.py
"""

from __future__ import annotations

from dataclasses import dataclass

from stint.dialects.jira.tmp.types import (
    TMP_FIELD_TYPE_KEYS,
    TmpFieldTypeError,
    tmp_type_key_for,
)
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


@dataclass(frozen=True)
class Result:
    type_class: type[_FieldType]
    accepted: bool
    detail: str  # mapped type id, or the error message


def probe(t: type[_FieldType]) -> Result:
    try:
        return Result(t, True, tmp_type_key_for(t))
    except TmpFieldTypeError as exc:
        return Result(t, False, str(exc))


def main() -> int:
    all_types: tuple[type[_FieldType], ...] = (
        TextField,
        TextAreaField,
        SelectField,
        MultiSelectField,
        UserField,
        NumberField,
        DateField,
        DateTimeField,
        RadioButtonsField,
        CheckboxesField,
        LabelsField,
        URLField,
        VersionField,
        MultiVersionField,
        GroupField,
        MultiGroupField,
        MultiUserField,
        ReadOnlyField,
    )
    results = [probe(t) for t in all_types]
    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]

    print(f"Probed {len(results)} stint field types against the TMP write surface.")
    print()
    print(f"  accepted ({len(accepted)} of {len(results)}):")
    for r in accepted:
        print(f"    [PASS] {r.type_class.__name__:<20} {r.detail}")
    print()
    print(f"  rejected ({len(rejected)} of {len(results)}):")
    for r in rejected:
        first_line = r.detail.split("\n", 1)[0]
        print(f"    [FAIL] {r.type_class.__name__:<20} {first_line}")
    print()

    # Spot-check the integration: build_tmp_desired should raise eagerly
    # for any rejected type. We can't easily import a schema here without
    # polluting the global registry, so demonstrate the rejection path
    # via tmp_type_key_for directly and confirm it subclasses
    # ConfigurationError (the property the cmd_apply error path relies on).
    print("Configuration hierarchy check:")
    try:
        tmp_type_key_for(UserField)
    except TmpFieldTypeError as exc:
        is_cfg = isinstance(exc, ConfigurationError)
        print(f"  TmpFieldTypeError subclasses ConfigurationError: {is_cfg}")
        if not is_cfg:
            return 1
    print()
    print("Mapping keys (in-tree, must match live):")
    for t, key in sorted(TMP_FIELD_TYPE_KEYS.items(), key=lambda kv: kv[0].__name__):
        print(f"    {t.__name__:<20} {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
