"""Coverage for the Jira custom field types added to close out GH #15.

Two things to check per type:
  - schema-plane: CustomField.__post_init__ options rules, and (for
    option-style types) the Literal-vs-options check in schema/issuetype.py.
  - data-plane: payload emission (write) and hydrate (read) round-trip the
    shape documented in stint/query/payload.py and stint/query/hydrate.py.

Deliberately not using `from __future__ import annotations` here -- see the
explanation at the top of test_m0_spike.py. Eager annotation evaluation lets
Annotated[Literal[...], some_cf] resolve `some_cf` from local scope.
"""

import sys
from typing import Annotated, Literal

import pytest

from stint import (
    CheckboxesField,
    ConfigurationError,
    CustomField,
    GroupField,
    IssueType,
    LabelsField,
    MultiGroupField,
    MultiUserField,
    MultiVersionField,
    RadioButtonsField,
    ReadOnlyField,
    StateFile,
    URLField,
    UserField,
    VersionField,
    registry,
)
from stint.query.hydrate import hydrate
from stint.query.payload import build_fields_payload
from stint.state.file import CustomFieldMapping


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform", None)


def _state_with(alias: str, field_id: str = "customfield_10099") -> StateFile:
    state = StateFile(env="dev", jira_url="https://jira.example.com")
    state.custom_fields[alias] = CustomFieldMapping(id=field_id)
    return state


# ── Option-style types: radiobuttons / checkboxes ─────────────────────
def test_radiobuttons_literal_matches_options_accepted():
    cf = CustomField(alias="rb_match", name="RB", type=RadioButtonsField, options=["A", "B"])

    class RBIT(IssueType):
        __alias__ = "rb_match_it"

        choice: Annotated[Literal["A", "B"], cf]


def test_radiobuttons_requires_options():
    with pytest.raises(ValueError, match="require options"):
        CustomField(alias="rb_no_opts", name="RB", type=RadioButtonsField)


def test_checkboxes_list_literal_accepted():
    cf = CustomField(alias="cb_match", name="CB", type=CheckboxesField, options=["x", "y", "z"])

    class CBIT(IssueType):
        __alias__ = "cb_match_it"

        choices: Annotated[list[Literal["x", "y", "z"]], cf]


def test_checkboxes_list_literal_mismatch_rejected():
    cf = CustomField(alias="cb_mismatch", name="CB", type=CheckboxesField, options=["x", "y"])
    with pytest.raises(ConfigurationError):

        class BadIT(IssueType):
            __alias__ = "cb_mismatch_it"

            choices: Annotated[list[Literal["x", "z"]], cf]


# ── Non-option types reject `options` ──────────────────────────────────
@pytest.mark.parametrize(
    "field_type",
    [
        LabelsField,
        URLField,
        VersionField,
        MultiVersionField,
        GroupField,
        MultiGroupField,
        MultiUserField,
        ReadOnlyField,
    ],
)
def test_non_select_style_type_rejects_options(field_type):
    with pytest.raises(ValueError, match="options only valid for select-style types"):
        CustomField(alias="bad", name="Bad", type=field_type, options=["A"])


# ── Payload + hydrate round-trips ──────────────────────────────────────
def test_radiobuttons_round_trip():
    cf = CustomField(alias="rb", name="RB", type=RadioButtonsField, options=["A", "B"])

    class RBIT(IssueType):
        __alias__ = "rb_it"

        choice: Annotated[Literal["A", "B"] | None, cf] = None

    state = _state_with("rb")
    instance = RBIT(choice="A")
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == {"value": "A"}

    back = hydrate(RBIT, {"key": "P-1", "fields": {"customfield_10099": {"value": "A"}}}, state)
    assert back.choice == "A"


def test_checkboxes_round_trip():
    cf = CustomField(alias="cb", name="CB", type=CheckboxesField, options=["x", "y"])

    class CBIT(IssueType):
        __alias__ = "cb_it"

        choices: Annotated[list[Literal["x", "y"]] | None, cf] = None

    state = _state_with("cb")
    instance = CBIT(choices=["x", "y"])
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == [{"value": "x"}, {"value": "y"}]

    raw = {"key": "P-1", "fields": {"customfield_10099": [{"value": "x"}, {"value": "y"}]}}
    back = hydrate(CBIT, raw, state)
    assert back.choices == ["x", "y"]


def test_labels_round_trip():
    cf = CustomField(alias="labels", name="Labels", type=LabelsField)

    class LabelIT(IssueType):
        __alias__ = "label_it"

        tags: Annotated[list[str] | None, cf] = None

    state = _state_with("labels")
    instance = LabelIT(tags=["backend", "urgent"])
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == ["backend", "urgent"]

    raw = {"key": "P-1", "fields": {"customfield_10099": ["backend", "urgent"]}}
    back = hydrate(LabelIT, raw, state)
    assert back.tags == ["backend", "urgent"]


def test_url_round_trip():
    cf = CustomField(alias="link", name="Link", type=URLField)

    class LinkIT(IssueType):
        __alias__ = "link_it"

        link: Annotated[str | None, cf] = None

    state = _state_with("link")
    instance = LinkIT(link="https://example.com")
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == "https://example.com"

    raw = {"key": "P-1", "fields": {"customfield_10099": "https://example.com"}}
    back = hydrate(LinkIT, raw, state)
    assert back.link == "https://example.com"


def test_readonly_round_trip():
    cf = CustomField(alias="ro", name="RO", type=ReadOnlyField)

    class ROIT(IssueType):
        __alias__ = "ro_it"

        note: Annotated[str | None, cf] = None

    state = _state_with("ro")
    instance = ROIT(note="computed value")
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == "computed value"


def test_version_round_trip():
    cf = CustomField(alias="version", name="Version", type=VersionField)

    class VersionIT(IssueType):
        __alias__ = "version_it"

        v: Annotated[str | None, cf] = None

    state = _state_with("version")
    instance = VersionIT(v="1.0")
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == {"name": "1.0"}

    raw = {"key": "P-1", "fields": {"customfield_10099": {"name": "1.0"}}}
    back = hydrate(VersionIT, raw, state)
    assert back.v == "1.0"


def test_multiversion_round_trip():
    cf = CustomField(alias="multiversion", name="Versions", type=MultiVersionField)

    class MultiVersionIT(IssueType):
        __alias__ = "multiversion_it"

        vs: Annotated[list[str] | None, cf] = None

    state = _state_with("multiversion")
    instance = MultiVersionIT(vs=["1.0", "2.0"])
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == [{"name": "1.0"}, {"name": "2.0"}]

    raw = {"key": "P-1", "fields": {"customfield_10099": [{"name": "1.0"}, {"name": "2.0"}]}}
    back = hydrate(MultiVersionIT, raw, state)
    assert back.vs == ["1.0", "2.0"]


def test_group_round_trip():
    cf = CustomField(alias="group", name="Group", type=GroupField)

    class GroupIT(IssueType):
        __alias__ = "group_it"

        g: Annotated[str | None, cf] = None

    state = _state_with("group")
    instance = GroupIT(g="jira-admins")
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == {"name": "jira-admins"}

    raw = {"key": "P-1", "fields": {"customfield_10099": {"name": "jira-admins"}}}
    back = hydrate(GroupIT, raw, state)
    assert back.g == "jira-admins"


def test_multigroup_round_trip():
    cf = CustomField(alias="multigroup", name="Groups", type=MultiGroupField)

    class MultiGroupIT(IssueType):
        __alias__ = "multigroup_it"

        gs: Annotated[list[str] | None, cf] = None

    state = _state_with("multigroup")
    instance = MultiGroupIT(gs=["jira-admins", "devs"])
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == [{"name": "jira-admins"}, {"name": "devs"}]

    raw = {"key": "P-1", "fields": {"customfield_10099": [{"name": "jira-admins"}, {"name": "devs"}]}}
    back = hydrate(MultiGroupIT, raw, state)
    assert back.gs == ["jira-admins", "devs"]


def test_multiuser_round_trip_cloud_and_dc():
    cf = CustomField(alias="multiuser", name="Users", type=MultiUserField)

    class MultiUserIT(IssueType):
        __alias__ = "multiuser_it"

        watchers: Annotated[list[str] | None, cf] = None

    state = _state_with("multiuser")
    instance = MultiUserIT(watchers=["acc-1", "acc-2"])
    cloud = build_fields_payload(instance, state, is_cloud=True)
    dc = build_fields_payload(instance, state, is_cloud=False)
    assert cloud["customfield_10099"] == [{"accountId": "acc-1"}, {"accountId": "acc-2"}]
    assert dc["customfield_10099"] == [{"name": "acc-1"}, {"name": "acc-2"}]

    raw = {"key": "P-1", "fields": {"customfield_10099": [{"accountId": "acc-1"}, {"name": "acc-2"}]}}
    back = hydrate(MultiUserIT, raw, state)
    assert back.watchers == ["acc-1", "acc-2"]


def test_user_field_unaffected_by_new_types():
    """Sanity check: existing UserField behavior is untouched by this pass."""
    cf = CustomField(alias="user", name="User", type=UserField)

    class UserIT(IssueType):
        __alias__ = "user_it"

        owner: Annotated[str | None, cf] = None

    state = _state_with("user")
    instance = UserIT(owner="acc-9")
    fields = build_fields_payload(instance, state, is_cloud=True)
    assert fields["customfield_10099"] == {"accountId": "acc-9"}
