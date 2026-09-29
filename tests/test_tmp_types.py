"""TMP custom-field type vocabulary: mapping + eager validation.

The TMP write surface accepts only a subset of Jira's full custom-field
type ids -- 9 of 18, confirmed live (see ``ISSUE_DRAFT.md`` TMP-B and
``stint/dialects/jira/tmp/types.py``). These tests pin the mapping so a
silent vocabulary drift cannot slip in, and verify the validator raises
``TmpFieldTypeError`` (a ``ConfigurationError`` subclass) at plan time for
unsupported types.

Note: deliberately does not use ``from __future__ import annotations``.
Pydantic's model_fields resolution inside an IssueType subclass defined
inside a function gets the ``Annotated`` metadata wrong when annotations
are stringified (confirmed empirically -- adding the future import here
makes the custom_field_map come back empty). The sibling file
``test_tmp_reconcile.py`` also avoids the import for the same reason.
"""

from typing import Annotated, Literal

import pytest

from stint import CustomField, IssueType, Project
from stint.dialects.jira.tmp.desired import build_tmp_desired
from stint.dialects.jira.tmp.types import (
    TMP_FIELD_TYPE_KEYS,
    TMP_TYPE_KEY_PREFIX,
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
)
from stint.registry import registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Other test modules leave IssueType/Project/CustomField instances in
    the process-wide registry; reset around each test so duplicate-alias
    errors don't bleed across modules."""
    registry.reset()
    yield
    registry.reset()


# ── mapping shape ──────────────────────────────────────────────────
def test_mapping_contains_every_live_confirmed_type():
    """The 9 types confirmed live on a real tenant are all in the map.

    Pinned individually (not as a count) so a future addition shows up
    clearly in the test diff instead of as an off-by-one delta on a
    single number.
    """
    expected = {
        TextField,
        TextAreaField,
        SelectField,
        CheckboxesField,
        LabelsField,
        URLField,
        NumberField,
        DateField,
        DateTimeField,
    }
    assert set(TMP_FIELD_TYPE_KEYS) == expected


def test_each_mapped_type_key_uses_the_canonical_prefix():
    """Atlassian's system custom-field type ids all share this prefix; a
    single string in one place keeps it greppable."""
    for key in TMP_FIELD_TYPE_KEYS.values():
        assert key.startswith(TMP_TYPE_KEY_PREFIX), f"{key!r} missing prefix"


def test_supported_types_round_trip_through_lookup():
    """Every supported type resolves to a non-empty Jira type id."""
    for field_type in TMP_FIELD_TYPE_KEYS:
        key = tmp_type_key_for(field_type)
        assert key and key.startswith(TMP_TYPE_KEY_PREFIX)


# ── eager error on unsupported ─────────────────────────────────────
UNSUPPORTED_TYPES = (
    MultiSelectField,
    UserField,
    ReadOnlyField,
    RadioButtonsField,
    GroupField,
    MultiGroupField,
    MultiUserField,
    VersionField,
    MultiVersionField,
)


@pytest.mark.parametrize("unsupported_type", UNSUPPORTED_TYPES)
def test_unsupported_type_raises_tmp_field_type_error(unsupported_type):
    """The full live-failing set raises eagerly, with a message naming
    the type and pointing at the supported set."""
    with pytest.raises(TmpFieldTypeError) as exc_info:
        tmp_type_key_for(unsupported_type)
    msg = str(exc_info.value)
    assert unsupported_type.__name__ in msg
    assert "not supported by the TMP write surface" in msg
    # The supported set is enumerated so a schema author knows their options.
    assert "TextField" in msg
    assert "SelectField" in msg


def test_tmp_field_type_error_is_a_configuration_error():
    """Subclassing ConfigurationError keeps it on the same error-rendering
    path as any other schema misconfiguration -- the user fixes the
    schema, not the wire, and ``stint apply`` reports it without an
    unsightly traceback."""
    with pytest.raises(ConfigurationError):
        tmp_type_key_for(UserField)


# ── build_tmp_desired integration ──────────────────────────────────
def test_build_tmp_desired_runs_mapping_for_supported_type():
    """Sanity: a schema with only supported types walks through the
    mapping without raising and produces a TmpDesiredField with the
    expected type_key (one whose Jira id matches the mapped entry)."""
    sev_cf = CustomField(alias="sev", name="Sev", type=SelectField, options=["S1", "S2"])

    class _Bug(IssueType):
        __alias__ = "bug"
        sev: Annotated[Literal["S1", "S2"], sev_cf]

    class _VM(Project):
        __key__ = "VM"
        __style__ = "team-managed"
        __issuetypes__ = [_Bug]

    desired = build_tmp_desired(_VM)
    assert desired.fields["sev"].type_key == TMP_FIELD_TYPE_KEYS[SelectField]


def test_build_tmp_desired_raises_eagerly_for_unsupported_type():
    """The point of the eager check: a schema declaring a type the TMP
    mutation rejects (UserField, confirmed live) fails at plan time with
    a precise message, not on a live 400 from
    createCustomFieldInProjectAndAddToAllIssueTypes."""
    owner_cf = CustomField(alias="owner", name="Owner", type=UserField)

    class _Bug(IssueType):
        __alias__ = "bug"
        owner: Annotated[str | None, owner_cf]

    class _VM(Project):
        __key__ = "VM"
        __style__ = "team-managed"
        __issuetypes__ = [_Bug]

    with pytest.raises(TmpFieldTypeError) as exc_info:
        build_tmp_desired(_VM)
    msg = str(exc_info.value)
    assert "owner" in msg or "UserField" in msg
