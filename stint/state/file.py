"""Per-environment state file: alias -> Jira ID mappings, committed to git.

Format (YAML) per plan Section 5. Schema version: 1.

Mappings cover every admin object stint manages. Each alias resolves to a
type-specific mapping carrying the Jira ID and (where relevant) per-element
sub-mappings — custom field options, screen tabs, scheme entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from stint.exceptions import StateFileCorruptError

SCHEMA_VERSION = 1


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is an empty dict, list, or string. Keeps YAML tidy."""
    return {k: v for k, v in d.items() if v not in ({}, [], "")}


@dataclass
class CustomFieldMapping:
    id: str
    options: dict[str, str] = field(default_factory=dict)  # option name -> option id

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty({"id": self.id, "options": dict(self.options)})

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CustomFieldMapping:
        return cls(id=raw["id"], options=dict(raw.get("options", {})))


@dataclass
class ScreenMapping:
    id: str
    tab_ids: dict[str, str] = field(default_factory=dict)  # tab name -> tab id

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty({"id": self.id, "tab_ids": dict(self.tab_ids)})

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScreenMapping:
        return cls(id=raw["id"], tab_ids=dict(raw.get("tab_ids", {})))


@dataclass
class SimpleMapping:
    """For objects with no sub-mappings: issuetypes, screen schemes,
    issuetype screen schemes, field configurations, field config schemes."""

    id: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SimpleMapping:
        return cls(id=raw["id"])


@dataclass
class ProjectMapping:
    """Projects carry style (CMP vs TMP) so runtime ops can branch. Defaults
    to ``"company-managed"`` for back-compat with state files written before M7."""

    id: str
    style: str = "company-managed"  # or "team-managed"
    key: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id}
        if self.style and self.style != "company-managed":
            out["style"] = self.style
        if self.key:
            out["key"] = self.key
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectMapping:
        return cls(
            id=raw["id"],
            style=raw.get("style", "company-managed"),
            key=raw.get("key", ""),
        )


@dataclass
class StateFile:
    env: str
    jira_url: str
    jira_version: str = ""
    deployment_type: str = ""
    last_applied: str | None = None
    schema_version: int = SCHEMA_VERSION
    revision: str | None = None  # current migration revision id, None = base
    custom_fields: dict[str, CustomFieldMapping] = field(default_factory=dict)
    issuetypes: dict[str, SimpleMapping] = field(default_factory=dict)
    projects: dict[str, ProjectMapping] = field(default_factory=dict)
    screens: dict[str, ScreenMapping] = field(default_factory=dict)
    screen_schemes: dict[str, SimpleMapping] = field(default_factory=dict)
    issuetype_schemes: dict[str, SimpleMapping] = field(default_factory=dict)
    issuetype_screen_schemes: dict[str, SimpleMapping] = field(default_factory=dict)
    field_configurations: dict[str, SimpleMapping] = field(default_factory=dict)
    field_configuration_schemes: dict[str, SimpleMapping] = field(default_factory=dict)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self._to_dict(), sort_keys=False)

    def _to_dict(self) -> dict[str, Any]:
        mappings = _drop_empty(
            {
                "custom_fields": {a: m.to_dict() for a, m in self.custom_fields.items()},
                "issuetypes": {a: m.to_dict() for a, m in self.issuetypes.items()},
                "projects": {a: m.to_dict() for a, m in self.projects.items()},
                "screens": {a: m.to_dict() for a, m in self.screens.items()},
                "screen_schemes": {a: m.to_dict() for a, m in self.screen_schemes.items()},
                "issuetype_schemes": {a: m.to_dict() for a, m in self.issuetype_schemes.items()},
                "issuetype_screen_schemes": {a: m.to_dict() for a, m in self.issuetype_screen_schemes.items()},
                "field_configurations": {a: m.to_dict() for a, m in self.field_configurations.items()},
                "field_configuration_schemes": {a: m.to_dict() for a, m in self.field_configuration_schemes.items()},
            }
        )
        return {
            "schema_version": self.schema_version,
            "env": self.env,
            "jira_url": self.jira_url,
            "jira_version": self.jira_version,
            "deployment_type": self.deployment_type,
            "revision": self.revision,
            "last_applied": self.last_applied,
            "mappings": mappings,
        }

    @classmethod
    def from_yaml(cls, text: str) -> StateFile:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise StateFileCorruptError(f"state file is not valid YAML: {e}") from e
        if not isinstance(raw, dict):
            raise StateFileCorruptError("state file root must be a mapping")
        sv = raw.get("schema_version", SCHEMA_VERSION)
        if sv != SCHEMA_VERSION:
            raise StateFileCorruptError(f"state file schema_version {sv} not understood; expected {SCHEMA_VERSION}")
        m = raw.get("mappings") or {}
        return cls(
            env=raw["env"],
            jira_url=raw["jira_url"],
            jira_version=raw.get("jira_version", ""),
            deployment_type=raw.get("deployment_type", ""),
            last_applied=raw.get("last_applied"),
            revision=raw.get("revision"),
            schema_version=sv,
            custom_fields={a: CustomFieldMapping.from_dict(d) for a, d in (m.get("custom_fields") or {}).items()},
            issuetypes={a: SimpleMapping.from_dict(d) for a, d in (m.get("issuetypes") or {}).items()},
            projects={a: ProjectMapping.from_dict(d) for a, d in (m.get("projects") or {}).items()},
            screens={a: ScreenMapping.from_dict(d) for a, d in (m.get("screens") or {}).items()},
            screen_schemes={a: SimpleMapping.from_dict(d) for a, d in (m.get("screen_schemes") or {}).items()},
            issuetype_schemes={a: SimpleMapping.from_dict(d) for a, d in (m.get("issuetype_schemes") or {}).items()},
            issuetype_screen_schemes={
                a: SimpleMapping.from_dict(d) for a, d in (m.get("issuetype_screen_schemes") or {}).items()
            },
            field_configurations={
                a: SimpleMapping.from_dict(d) for a, d in (m.get("field_configurations") or {}).items()
            },
            field_configuration_schemes={
                a: SimpleMapping.from_dict(d) for a, d in (m.get("field_configuration_schemes") or {}).items()
            },
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_yaml())

    @classmethod
    def load(cls, path: str | Path) -> StateFile:
        p = Path(path)
        if not p.exists():
            raise StateFileCorruptError(f"state file does not exist: {p}")
        return cls.from_yaml(p.read_text())
