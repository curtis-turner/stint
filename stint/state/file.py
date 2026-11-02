"""Per-environment state file: alias -> Jira ID mappings, committed to git.

Format (YAML) per plan Section 5. Schema version: 1.

Mappings cover every admin object stint manages. Each alias resolves to a
type-specific mapping carrying the Jira ID and (where relevant) per-element
sub-mappings — custom field options, screen tabs, scheme entries.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from stint.exceptions import ConfigurationError, StateFileCorruptError, StateFileWriteError

SCHEMA_VERSION = 1


def _normalize_target_url(url: str) -> str:
    """Compare form for env/URL mismatch checks (M7): strip a dialect prefix
    (state files written before that normalization may carry one) and any
    trailing slash."""
    head, _, tail = url.partition("://")
    if tail and "+" in head:
        url = f"{head.split('+', 1)[1]}://{tail}"
    return url.rstrip("/")


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is an empty dict, list, or string. Keeps YAML tidy."""
    return {k: v for k, v in d.items() if v not in ({}, [], "")}


def _required(raw: dict[str, Any], key: str, where: str) -> Any:
    """Read a required key from a state-file mapping (M8).

    A corrupt/incomplete state file must surface as ``StateFileCorruptError``
    naming the missing path — a bare ``KeyError`` escapes the CLI's
    ``StintError`` handling and prints a raw traceback."""
    if not isinstance(raw, dict):
        raise StateFileCorruptError(f"state file is corrupt: {where} must be a mapping, got {type(raw).__name__}")
    if key not in raw:
        raise StateFileCorruptError(f"state file is corrupt: {where} is missing required key {key!r}")
    return raw[key]


@dataclass
class CustomFieldMapping:
    id: str
    options: dict[str, str] = field(default_factory=dict)  # option name -> option id

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty({"id": self.id, "options": dict(self.options)})

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "<custom field mapping>") -> CustomFieldMapping:
        return cls(id=_required(raw, "id", where), options=dict(raw.get("options", {})))


@dataclass
class ScreenMapping:
    id: str
    tab_ids: dict[str, str] = field(default_factory=dict)  # tab name -> tab id

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty({"id": self.id, "tab_ids": dict(self.tab_ids)})

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "<screen mapping>") -> ScreenMapping:
        return cls(id=_required(raw, "id", where), tab_ids=dict(raw.get("tab_ids", {})))


@dataclass
class SimpleMapping:
    """For objects with no sub-mappings: issuetypes, screen schemes,
    issuetype screen schemes, field configurations, field config schemes."""

    id: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "<mapping>") -> SimpleMapping:
        return cls(id=_required(raw, "id", where))


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
    def from_dict(cls, raw: dict[str, Any], *, where: str = "<project mapping>") -> ProjectMapping:
        return cls(
            id=_required(raw, "id", where),
            style=raw.get("style", "company-managed"),
            key=raw.get("key", ""),
        )


@dataclass
class SecuritySchemeMapping:
    """One alias -> Jira issue-security-scheme binding, plus level ids.

    ``level_ids`` maps a declared level designation (``SecurityLevel.id``, or
    ``name`` when undeclared) to Jira's level id. Jira's security-scheme write
    endpoints return no level ids (204 bodies), so ids are captured by a
    scheme GET right after each create/add and matched by level name when a
    stored id is missing (names must be unique within a scheme).
    ``member_values`` records the *resolved* member parameters (accountIds /
    group names) each level's members were set with, so re-runs are
    idempotent even after email → accountId resolution.
    """

    id: str
    default_level_id: str | None = None
    level_ids: dict[str, str] = field(default_factory=dict)  # designation -> jira level id
    member_values: dict[str, list[str]] = field(default_factory=dict)  # designation -> resolved params

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id}
        if self.default_level_id:
            out["default_level_id"] = self.default_level_id
        if self.level_ids:
            out["level_ids"] = dict(self.level_ids)
        if self.member_values:
            out["member_values"] = {k: sorted(v) for k, v in self.member_values.items()}
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "<security scheme mapping>") -> SecuritySchemeMapping:
        return cls(
            id=_required(raw, "id", where),
            default_level_id=raw.get("default_level_id"),
            level_ids={str(k): str(v) for k, v in (raw.get("level_ids") or {}).items()},
            member_values={str(k): [str(v) for v in values] for k, values in (raw.get("member_values") or {}).items()},
        )


@dataclass
class TmpProjectState:
    """One team-managed project's alias -> Jira ID mappings.

    Kept as a generic, TMP-shaped record here (not imported from
    ``stint.dialects.jira.tmp``, per that package's isolation rule: core must
    never import from it, only the reverse). ``layout_ids`` is carried for
    diagnostics only (L27): ``SetLayout`` always takes its layout id from the
    fresh snapshot or a new ``read_layout``, never from this record — the
    internal API hands the layout back on every read.
    """

    fields: dict[str, str] = field(default_factory=dict)  # alias -> fieldId
    worktypes: dict[str, str] = field(default_factory=dict)  # alias -> issuetype id
    layout_ids: dict[str, str] = field(default_factory=dict)  # worktype alias -> layoutId

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "fields": dict(self.fields),
                "worktypes": dict(self.worktypes),
                "layout_ids": dict(self.layout_ids),
            }
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "<tmp project state>") -> TmpProjectState:
        # No required keys (all sections optional), but a non-mapping value is
        # still corruption and must not escape as an AttributeError.
        if not isinstance(raw, dict):
            raise StateFileCorruptError(f"state file is corrupt: {where} must be a mapping, got {type(raw).__name__}")
        return cls(
            fields=dict(raw.get("fields", {})),
            worktypes=dict(raw.get("worktypes", {})),
            layout_ids=dict(raw.get("layout_ids", {})),
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
    security_schemes: dict[str, SecuritySchemeMapping] = field(default_factory=dict)
    tmp_projects: dict[str, TmpProjectState] = field(default_factory=dict)  # project alias -> TMP state

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
                "security_schemes": {a: m.to_dict() for a, m in self.security_schemes.items()},
                "tmp_projects": {a: m.to_dict() for a, m in self.tmp_projects.items()},
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
            env=_required(raw, "env", "state file root"),
            jira_url=_required(raw, "jira_url", "state file root"),
            jira_version=raw.get("jira_version", ""),
            deployment_type=raw.get("deployment_type", ""),
            last_applied=raw.get("last_applied"),
            revision=raw.get("revision"),
            schema_version=sv,
            custom_fields={
                a: CustomFieldMapping.from_dict(d, where=f"mappings.custom_fields[{a!r}]")
                for a, d in (m.get("custom_fields") or {}).items()
            },
            issuetypes={
                a: SimpleMapping.from_dict(d, where=f"mappings.issuetypes[{a!r}]")
                for a, d in (m.get("issuetypes") or {}).items()
            },
            projects={
                a: ProjectMapping.from_dict(d, where=f"mappings.projects[{a!r}]")
                for a, d in (m.get("projects") or {}).items()
            },
            screens={
                a: ScreenMapping.from_dict(d, where=f"mappings.screens[{a!r}]")
                for a, d in (m.get("screens") or {}).items()
            },
            screen_schemes={
                a: SimpleMapping.from_dict(d, where=f"mappings.screen_schemes[{a!r}]")
                for a, d in (m.get("screen_schemes") or {}).items()
            },
            issuetype_schemes={
                a: SimpleMapping.from_dict(d, where=f"mappings.issuetype_schemes[{a!r}]")
                for a, d in (m.get("issuetype_schemes") or {}).items()
            },
            issuetype_screen_schemes={
                a: SimpleMapping.from_dict(d, where=f"mappings.issuetype_screen_schemes[{a!r}]")
                for a, d in (m.get("issuetype_screen_schemes") or {}).items()
            },
            field_configurations={
                a: SimpleMapping.from_dict(d, where=f"mappings.field_configurations[{a!r}]")
                for a, d in (m.get("field_configurations") or {}).items()
            },
            field_configuration_schemes={
                a: SimpleMapping.from_dict(d, where=f"mappings.field_configuration_schemes[{a!r}]")
                for a, d in (m.get("field_configuration_schemes") or {}).items()
            },
            security_schemes={
                a: SecuritySchemeMapping.from_dict(d, where=f"mappings.security_schemes[{a!r}]")
                for a, d in (m.get("security_schemes") or {}).items()
            },
            tmp_projects={
                a: TmpProjectState.from_dict(d, where=f"mappings.tmp_projects[{a!r}]")
                for a, d in (m.get("tmp_projects") or {}).items()
            },
        )

    def save(self, path: str | Path) -> None:
        """Atomically write the state file, creating parent directories.

        Content goes to a sibling temp file in the target's directory (so
        ``os.replace`` never crosses a filesystem), is fsync()ed, then is
        moved onto the target with ``os.replace`` -- atomic on POSIX and
        Windows. A crash, Ctrl-C, or full disk mid-write leaves either the
        previous complete file or the complete new one, never a truncated
        half-file: this file is the only record of alias -> Jira ID
        mappings. Failures raise ``StateFileWriteError`` (a ``StintError``),
        not a raw ``OSError``, so the CLI prints a message rather than a
        traceback.
        """
        p = Path(path)
        StateFile.ensure_writable(p)
        try:
            fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(self.to_yaml())
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, p)
            except BaseException:
                # Never strand a partial temp file beside the state file.
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as e:
            raise StateFileWriteError(f"failed to write state file {p}: {e}") from e

    @staticmethod
    def ensure_writable(path: str | Path) -> None:
        """Fail fast if the state file path cannot be written.

        Creates parent directories (``stint upgrade --state state/dev.yaml``
        previously mutated Jira first and only then died on the missing
        ``state/``) and proves the directory accepts files by creating and
        removing a throwaway temp file -- the same thing ``save()`` does, so
        this is a true preflight. Call this before issuing any Jira writes.
        """
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.probe-", suffix=".tmp")
            os.close(fd)
            os.unlink(probe)
        except OSError as e:
            raise StateFileWriteError(f"state file path {p} is not writable: {e}") from e

    @classmethod
    def load(cls, path: str | Path) -> StateFile:
        p = Path(path)
        if not p.exists():
            raise StateFileCorruptError(f"state file does not exist: {p}")
        return cls.from_yaml(p.read_text(encoding="utf-8"))

    def validate_target(self, *, env: str, jira_url: str, state_path: str | Path) -> None:
        """Refuse to run against a connection that disagrees with this file (M7).

        This file's id mappings belong to the tenant/env recorded when it was
        created. Pointing a prod state file at dev — or a typo'd ``--url`` —
        runs migrations on the wrong tenant using the other's ids, and
        name-based matching then binds wrong-tenant ids. Recorded URLs are
        compared modulo dialect prefix and trailing slash, so state files
        written before that normalization keep validating."""
        recorded_url = _normalize_target_url(self.jira_url)
        incoming = _normalize_target_url(jira_url)
        if self.env != env:
            raise ConfigurationError(
                f"state file {state_path} records env {self.env!r}, not {env!r}. "
                f"This file's id mappings belong to the recorded tenant/env; rerun "
                f"with --env {self.env}, or point --state at a separate file for "
                f"env {env!r}."
            )
        if recorded_url != incoming:
            raise ConfigurationError(
                f"state file {state_path} was written against {self.jira_url!r}, "
                f"but you are connecting to {jira_url!r}. Running migrations on one "
                f"tenant with another tenant's id mappings can create or clobber "
                f"project objects. Point --url at {self.jira_url!r}, or point "
                f"--state at a file for the target tenant."
            )
