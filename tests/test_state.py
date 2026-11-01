"""State file load/save round-trip."""

import os
from pathlib import Path

import pytest

from stint import StateFile, StateFileCorruptError, StateFileWriteError
from stint.state.file import CustomFieldMapping, ProjectMapping, ScreenMapping, SimpleMapping


def test_state_file_roundtrip_yaml():
    sf = StateFile(
        env="prod",
        jira_url="https://jira.example.com",
        jira_version="9.12.4",
        deployment_type="Cloud",
        last_applied="2026-05-19T14:22:11Z",
        custom_fields={
            "bug_severity": CustomFieldMapping(
                id="customfield_10042",
                options={"S1": "10100", "S2": "10101", "S3": "10102", "S4": "10103"},
            ),
            "bug_root_cause": CustomFieldMapping(id="customfield_10043"),
        },
    )
    text = sf.to_yaml()
    parsed = StateFile.from_yaml(text)
    assert parsed.env == sf.env
    assert parsed.jira_url == sf.jira_url
    assert parsed.jira_version == sf.jira_version
    assert parsed.deployment_type == sf.deployment_type
    assert parsed.last_applied == sf.last_applied
    assert set(parsed.custom_fields) == {"bug_severity", "bug_root_cause"}
    assert parsed.custom_fields["bug_severity"].id == "customfield_10042"
    assert parsed.custom_fields["bug_severity"].options["S1"] == "10100"
    assert parsed.custom_fields["bug_root_cause"].options == {}


def test_state_file_save_load_round_trip(tmp_path: Path):
    sf = StateFile(
        env="staging",
        jira_url="https://jira-staging.example.com",
        custom_fields={"x": CustomFieldMapping(id="customfield_99999")},
    )
    target = tmp_path / "staging.yaml"
    sf.save(target)
    loaded = StateFile.load(target)
    assert loaded.env == "staging"
    assert loaded.custom_fields["x"].id == "customfield_99999"


# ── H2: atomic, parent-dir-creating save() ────────────────────────────


def test_state_file_save_creates_parent_dirs(tmp_path: Path):
    """`stint upgrade --state state/dev.yaml` works when state/ is absent."""
    sf = StateFile(env="dev", jira_url="https://dev.example.com")
    target = tmp_path / "state" / "dev.yaml"
    sf.save(target)
    assert target.exists()
    assert StateFile.load(target).env == "dev"


def test_state_file_save_overwrites_and_leaves_no_temp_files(tmp_path: Path):
    save_dir = tmp_path / "state"
    sf = StateFile(env="dev", jira_url="https://dev.example.com")
    target = save_dir / "dev.yaml"
    sf.save(target)
    StateFile(env="prod", jira_url="https://prod.example.com").save(target)
    assert StateFile.load(target).env == "prod"
    assert [q.name for q in save_dir.iterdir()] == ["dev.yaml"]


def test_state_file_save_mid_write_crash_keeps_previous_file(tmp_path: Path, monkeypatch):
    """A failure between temp-file creation and os.replace must leave the
    previously saved state file byte-identical, with no temp file stranded."""

    def boom(_fileno: int) -> None:
        raise OSError("disk full")

    sf = StateFile(env="dev", jira_url="https://dev.example.com")
    target = tmp_path / "dev.yaml"
    sf.save(target)
    original = target.read_text(encoding="utf-8")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(StateFileWriteError):
        sf.save(target)

    assert target.read_text(encoding="utf-8") == original
    assert [q.name for q in tmp_path.iterdir()] == ["dev.yaml"]


def test_state_file_save_unwritable_path_raises_stint_error(tmp_path: Path):
    """OSError surfaces as StateFileWriteError, not a raw traceback."""
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory", encoding="utf-8")
    sf = StateFile(env="dev", jira_url="https://dev.example.com")
    with pytest.raises(StateFileWriteError) as e:
        sf.save(blocker / "state" / "dev.yaml")
    assert "dev.yaml" in str(e.value)


def test_state_file_non_ascii_round_trips_utf8(tmp_path: Path):
    sf = StateFile(env="devé", jira_url="https://dé.example.com")
    target = tmp_path / "dev.yaml"
    sf.save(target)
    parsed = StateFile.load(target)
    assert parsed.env == "devé"
    assert parsed.jira_url == "https://dé.example.com"


def test_state_file_missing_path_raises():
    with pytest.raises(StateFileCorruptError):
        StateFile.load("/no/such/file/anywhere.yaml")


def test_state_file_unknown_schema_version_rejected():
    bad = "schema_version: 99\nenv: prod\njira_url: x\nmappings:\n  custom_fields: {}\n"
    with pytest.raises(StateFileCorruptError) as e:
        StateFile.from_yaml(bad)
    assert "schema_version" in str(e.value)


def test_state_file_garbage_input_rejected():
    with pytest.raises(StateFileCorruptError):
        StateFile.from_yaml("[ this is not a mapping ]")


def test_empty_custom_fields_round_trips():
    sf = StateFile(env="dev", jira_url="https://dev.example.com")
    text = sf.to_yaml()
    parsed = StateFile.from_yaml(text)
    assert parsed.custom_fields == {}


# ── All admin object mappings round-trip ──────────────────────────────
def test_full_state_file_roundtrip():
    """Every admin mapping type the planner cares about survives YAML round-trip."""
    sf = StateFile(
        env="prod",
        jira_url="https://jira.example.com",
        jira_version="9.12.4",
        deployment_type="Cloud",
        custom_fields={
            "bug_severity": CustomFieldMapping(
                id="customfield_10042",
                options={"S1": "10100", "S2": "10101"},
            ),
        },
        issuetypes={"bug": SimpleMapping(id="10001")},
        projects={"PLAT": ProjectMapping(id="10000")},
        screens={
            "bug_create": ScreenMapping(id="10100", tab_ids={"Field Tab": "11000"}),
        },
        screen_schemes={"bug_screens": SimpleMapping(id="10200")},
        issuetype_screen_schemes={"bug_itss": SimpleMapping(id="10300")},
        field_configurations={"bug_fields": SimpleMapping(id="10400")},
        field_configuration_schemes={"bug_fcs": SimpleMapping(id="10500")},
    )
    parsed = StateFile.from_yaml(sf.to_yaml())
    assert parsed.custom_fields["bug_severity"].id == "customfield_10042"
    assert parsed.custom_fields["bug_severity"].options == {"S1": "10100", "S2": "10101"}
    assert parsed.issuetypes["bug"].id == "10001"
    assert parsed.projects["PLAT"].id == "10000"
    assert parsed.screens["bug_create"].id == "10100"
    assert parsed.screens["bug_create"].tab_ids == {"Field Tab": "11000"}
    assert parsed.screen_schemes["bug_screens"].id == "10200"
    assert parsed.issuetype_screen_schemes["bug_itss"].id == "10300"
    assert parsed.field_configurations["bug_fields"].id == "10400"
    assert parsed.field_configuration_schemes["bug_fcs"].id == "10500"


def test_empty_mapping_sections_omitted_from_yaml():
    """A state file with no custom fields should not emit a `custom_fields: {}`."""
    sf = StateFile(env="dev", jira_url="https://dev.example.com")
    text = sf.to_yaml()
    # Empty sections are dropped from the on-disk format to keep it readable.
    assert "custom_fields" not in text
    # But re-parsing still works and yields empty dicts.
    parsed = StateFile.from_yaml(text)
    assert parsed.custom_fields == {}
    assert parsed.projects == {}
