"""M0 spike validation.

Covers the five gates from plan_stint.md Section 17:

1. The declarative classes from Section 4 parse verbatim.
2. `stint.validate()` reports internal inconsistencies.
3. Pydantic validation rejects bad data without any network call.
4. The same class works as schema (registered in __issuetypes__) and as a data
   container (instantiable with valid data).
5. `Project.__style__` validation distinguishes CMP / TMP feature compatibility
   at class-definition time.
"""

import importlib
import sys
from typing import Annotated, Literal

import pytest
from pydantic import ValidationError

# Intentionally NOT using `from __future__ import annotations` in this test file.
# Future annotations defer type-hint resolution and Pydantic only sees module
# globals when it later resolves them; classes defined inside test functions
# would have unresolvable ForwardRefs because function-local CustomField
# objects are out of scope. Eager annotation evaluation sidesteps that.
from stint import (
    ConfigurationError,
    CustomField,
    IssueType,
    MultiSelectField,
    Project,
    Screen,
    ScreenScheme,
    SelectField,
    TextField,
    registry,
    validate,
)


def _load_example():
    """Re-import examples.platform from scratch against the current registry."""
    sys.modules.pop("examples.platform", None)
    return importlib.import_module("examples.platform")


@pytest.fixture(autouse=True)
def fresh_registry():
    """Each test starts with an empty registry."""
    registry.reset()
    sys.modules.pop("examples.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.platform", None)


# ── Gate 1: the example parses ────────────────────────────────────────
def test_example_module_parses():
    """examples/platform.py imports cleanly and populates the registry."""
    _load_example()
    assert "bug" in registry.issuetypes
    assert "PLAT" in registry.projects
    assert "bug_severity" in registry.custom_fields
    assert "bug_screens" in registry.screen_schemes
    assert "bug_fields" in registry.field_configurations


# ── Gate 3: Pydantic validates data without network ───────────────────
def test_issuetype_pydantic_rejects_bad_data():
    """Bad severity value raises Pydantic ValidationError, no network call."""
    mod = _load_example()
    with pytest.raises(ValidationError):
        mod.Bug(summary="x", reporter="cturner", severity="S5")  # type: ignore[arg-type]


def test_issuetype_pydantic_accepts_good_data():
    mod = _load_example()
    bug = mod.Bug(summary="x", reporter="cturner", severity="S2")
    assert bug.summary == "x"
    assert bug.severity == "S2"
    assert bug.key is None


def test_issuetype_pydantic_rejects_missing_required():
    """Missing 'summary' raises Pydantic ValidationError."""
    mod = _load_example()
    with pytest.raises(ValidationError):
        mod.Bug(reporter="cturner", severity="S2")  # type: ignore[call-arg]


# ── Gate 4: dual role (same class is schema + data) ───────────────────
def test_dual_role_same_class_used_for_both():
    """The Bug class is both registered as a schema object and instantiable as data."""
    mod = _load_example()
    assert mod.Bug in mod.Platform.__issuetypes__  # schema role
    instance = mod.Bug(summary="y", reporter="cturner", severity="S3")
    assert isinstance(instance, mod.Bug)  # data role
    assert instance.severity == "S3"


# ── Gate 2: validate() reports internal inconsistencies ───────────────
def test_validate_passes_for_valid_schema():
    _load_example()
    problems = validate()
    assert problems == [], f"unexpected validation problems: {problems}"


def test_validate_flags_screen_referencing_unregistered_custom_field():
    """A Screen that points at a CustomField not in the registry is reported."""
    # Build a synthetic invalid setup: declare a CustomField, register it, then
    # remove it from the registry to simulate the dangling reference.
    cf = CustomField(alias="ghost", name="Ghost", type=TextField)
    Screen(alias="s1", name="S1", fields=[cf])  # registers via constructor side effect
    # Remove the custom field from the registry to dangle the reference.
    del registry.custom_fields["ghost"]
    problems = validate()
    assert any("ghost" in p for p in problems)


def test_validate_flags_screenscheme_referencing_unregistered_screen():
    """A ScreenScheme pointing at an unregistered Screen is reported."""
    s = Screen(alias="s2", name="S2", fields=["Summary"])
    ss = ScreenScheme(alias="ss1", name="SS1", create=s, edit=s, view=s)
    del registry.screens["s2"]
    problems = validate()
    assert any("s2" in p for p in problems)
    assert ss.alias == "ss1"  # touch to avoid linter unused-variable noise


# ── Gate 5: __style__ validation at class-definition time ─────────────
def test_tmp_project_with_screen_scheme_rejected_at_definition_time():
    """A team-managed project that includes an issuetype with __screen_scheme__ fails."""
    screen = Screen(alias="t1", name="T1", fields=["Summary"])
    ss = ScreenScheme(alias="tss1", name="TSS1", create=screen, edit=screen, view=screen)

    class TmpBug(IssueType):
        __alias__ = "tmp_bug"
        __display_name__ = "TmpBug"
        __screen_scheme__ = ss

        summary: str

    with pytest.raises(ConfigurationError) as excinfo:

        class TmpProject(Project):
            __key__ = "TMP"
            __style__ = "team-managed"
            __issuetypes__ = [TmpBug]

    assert "team-managed" in str(excinfo.value).lower()
    assert "screenscheme" in str(excinfo.value).lower()


def test_tmp_project_without_schemes_accepted():
    """A TMP project whose issuetypes do not reference schemes is valid."""

    class TmpStory(IssueType):
        __alias__ = "tmp_story"
        __display_name__ = "TmpStory"

        summary: str

    class TmpProject2(Project):
        __key__ = "TMP2"
        __style__ = "team-managed"
        __issuetypes__ = [TmpStory]

    assert TmpProject2 in registry.projects.values()


def test_cmp_project_with_screen_scheme_accepted():
    """A company-managed project that uses schemes is valid (the common case)."""
    screen = Screen(alias="c1", name="C1", fields=["Summary"])
    ss = ScreenScheme(alias="css1", name="CSS1", create=screen, edit=screen, view=screen)

    class CmpBug(IssueType):
        __alias__ = "cmp_bug"
        __display_name__ = "CmpBug"
        __screen_scheme__ = ss

        summary: str

    class CmpProject(Project):
        __key__ = "CMP"
        __style__ = "company-managed"
        __issuetypes__ = [CmpBug]

    assert CmpProject in registry.projects.values()


# ── Bonus: defining IssueType without __alias__ fails ─────────────────
def test_issuetype_missing_alias_fails():
    with pytest.raises(ConfigurationError):

        class BadIssueType(IssueType):
            summary: str


def test_project_missing_key_fails():
    with pytest.raises(ConfigurationError):

        class BadProject(Project):
            __issuetypes__ = []  # also will fail, but key check comes first


def test_invalid_style_value_fails():
    class Story(IssueType):
        __alias__ = "story"

        summary: str

    with pytest.raises(ConfigurationError) as excinfo:

        class WeirdProject(Project):
            __key__ = "WEIRD"
            __style__ = "self-managed"  # type: ignore[assignment]
            __issuetypes__ = [Story]

    assert "invalid __style__" in str(excinfo.value)


# ── Annotated linkage between Pydantic fields and CustomFields ────────
def test_custom_field_map_populated_from_annotated_metadata():
    """Bug.__custom_field_map__ contains the CustomField objects linked via Annotated."""
    mod = _load_example()
    cf_map = mod.Bug.__custom_field_map__
    assert set(cf_map) == {"severity", "root_cause"}
    assert cf_map["severity"].alias == "bug_severity"
    assert cf_map["root_cause"].alias == "bug_root_cause"


def test_system_fields_not_in_custom_field_map():
    """System fields (no Annotated CustomField) do not appear in the map."""
    mod = _load_example()
    cf_map = mod.Bug.__custom_field_map__
    assert "summary" not in cf_map
    assert "reporter" not in cf_map
    assert "created" not in cf_map


# ── __title__ defaults to class name, overridable ─────────────────────
def test_title_defaults_to_class_name():
    """If __title__ is omitted, it falls back to the Python class name."""

    class Story(IssueType):
        __alias__ = "story"

        summary: str

    assert Story.__title__ == "Story"


def test_title_override_used_when_set():
    class CriticalBug(IssueType):
        __alias__ = "critical_bug"
        __title__ = "Critical Bug Report"

        summary: str

    assert CriticalBug.__title__ == "Critical Bug Report"


def test_project_title_defaults_to_class_name():
    class Story(IssueType):
        __alias__ = "p_story"

        summary: str

    class MyProj(Project):
        __key__ = "MYP"
        __issuetypes__ = [Story]

    assert MyProj.__title__ == "MyProj"


# ── Parse-time option/Literal consistency check ───────────────────────
# Convention enforced by these tests: module-level CustomField objects use a
# `_cf` suffix so they do not collide with the class attribute names used in
# Annotated[...]. Without the suffix, Pydantic's metadata processor stores the
# CustomField as an unresolvable ForwardRef and the check sees no CustomField
# at all.
def test_select_literal_matches_options_accepted():
    """Matching Literal and options pass without error."""
    sev_cf = CustomField(
        alias="sev_match",
        name="Sev",
        type=SelectField,
        options=["A", "B", "C"],
    )

    class SevIT(IssueType):
        __alias__ = "sev_match_it"

        sev: Annotated[Literal["A", "B", "C"], sev_cf]


def test_select_literal_subset_rejected():
    """Literal values that are a subset of options raises ConfigurationError."""
    sev_cf = CustomField(
        alias="sev_subset",
        name="Sev",
        type=SelectField,
        options=["A", "B", "C"],
    )
    with pytest.raises(ConfigurationError) as excinfo:

        class BadIT(IssueType):
            __alias__ = "sev_subset_it"

            sev: Annotated[Literal["A", "B"], sev_cf]

    assert "C" in str(excinfo.value)


def test_select_literal_superset_rejected():
    """Literal values that include extras not in options raises ConfigurationError."""
    sev_cf = CustomField(
        alias="sev_super",
        name="Sev",
        type=SelectField,
        options=["A", "B"],
    )
    with pytest.raises(ConfigurationError) as excinfo:

        class BadIT(IssueType):
            __alias__ = "sev_super_it"

            sev: Annotated[Literal["A", "B", "C"], sev_cf]

    assert "C" in str(excinfo.value)


def test_select_without_literal_rejected():
    """Bare str annotation on a select-style CustomField raises ConfigurationError."""
    sev_cf = CustomField(
        alias="sev_bare",
        name="Sev",
        type=SelectField,
        options=["A", "B"],
    )
    with pytest.raises(ConfigurationError) as excinfo:

        class BadIT(IssueType):
            __alias__ = "sev_bare_it"

            sev: Annotated[str, sev_cf]

    assert "Literal" in str(excinfo.value)


def test_select_literal_optional_accepted():
    """Optional[Literal[...]] with default None is accepted when Literal matches options."""
    sev_cf = CustomField(
        alias="sev_opt",
        name="Sev",
        type=SelectField,
        options=["A", "B"],
    )

    class OptIT(IssueType):
        __alias__ = "sev_opt_it"

        sev: Annotated[Literal["A", "B"] | None, sev_cf] = None


def test_multiselect_list_literal_accepted():
    """MultiSelect with list[Literal[...]] is accepted when values match."""
    tags_cf = CustomField(
        alias="tags_match",
        name="Tags",
        type=MultiSelectField,
        options=["red", "green", "blue"],
    )

    class TagsIT(IssueType):
        __alias__ = "tags_match_it"

        tags: Annotated[list[Literal["red", "green", "blue"]], tags_cf]


def test_multiselect_list_literal_mismatch_rejected():
    tags_cf = CustomField(
        alias="tags_mismatch",
        name="Tags",
        type=MultiSelectField,
        options=["red", "green", "blue"],
    )
    with pytest.raises(ConfigurationError):

        class BadIT(IssueType):
            __alias__ = "tags_mismatch_it"

            tags: Annotated[list[Literal["red", "yellow"]], tags_cf]


def test_text_field_skips_options_check():
    """TextField (no options) does not trigger the Literal check."""
    note_cf = CustomField(
        alias="note_skip",
        name="Note",
        type=TextField,
    )

    class NoteIT(IssueType):
        __alias__ = "note_skip_it"

        note: Annotated[str | None, note_cf] = None


def test_shadowed_customfield_name_raises_helpful_error():
    """When the CustomField shares a Python name with the class attribute, the
    definition is rejected.

    On Python 3.14+ (PEP 649 deferred annotations) pydantic hands the metaclass
    a ForwardRef, so stint raises a curated ConfigurationError pointing at the
    fix. On < 3.14 the unresolved name surfaces as a NameError during model
    construction instead -- still rejected, just without the tailored guidance.
    """
    sev = CustomField(  # noqa: F841 - intentionally same name as the attr
        alias="sev_shadow",
        name="Sev",
        type=SelectField,
        options=["A", "B"],
    )
    curated = sys.version_info >= (3, 14)
    with pytest.raises(ConfigurationError if curated else NameError) as excinfo:

        class ShadowIT(IssueType):
            __alias__ = "sev_shadow_it"

            sev: Annotated[Literal["A", "B"], sev]  # ty: ignore[unresolved-reference]

    if curated:
        msg = str(excinfo.value)
        assert "shadow" in msg.lower() or "forwardref" in msg.lower() or "_field" in msg or "_cf" in msg
