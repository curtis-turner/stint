"""In-memory snapshots of Jira state. Dialect-agnostic shape.

A snapshot is what the dialect produces when asked to reflect. The planner
diffs the desired schema against a snapshot to produce a plan of operations.
The state file persists the alias-to-Jira-ID mappings between runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerInfoSnapshot:
    """Subset of `GET /rest/api/{2,3}/serverInfo` that stint cares about."""

    deployment_type: str  # "Server" (DC) or "Cloud"
    version: str  # e.g. "9.12.4" or a Cloud build string
    base_url: str  # the server-reported base URL


@dataclass(frozen=True)
class CustomFieldSnapshot:
    """One Jira custom field, with its options if it is a select-style type."""

    id: str  # e.g. "customfield_10042"
    name: str  # display name in Jira
    type_id: str  # e.g. "...customfieldtypes:select"
    options: dict[str, str] = field(default_factory=dict)  # option name -> option id


@dataclass(frozen=True)
class IssueTypeSnapshot:
    """A Jira global issuetype (DC/CMP) or project-scoped issuetype (TMP)."""

    id: str
    name: str
    description: str = ""
    subtask: bool = False


@dataclass(frozen=True)
class ProjectSnapshot:
    """A Jira project. ``style`` is ``classic`` on DC and either
    ``classic`` (CMP) or ``next-gen`` (TMP) on Cloud."""

    id: str
    key: str
    name: str
    lead: str | None = None
    project_type_key: str = ""  # e.g. "software", "business"
    style: str = "classic"  # "classic" or "next-gen"
    # Resolved separately via /project/{key}/issuetypescreenscheme and
    # /project/{key}/fieldconfigurationscheme. Empty on TMP and on projects
    # using the global defaults.
    issuetype_scheme_id: str | None = None
    issuetype_screen_scheme_id: str | None = None
    field_configuration_scheme_id: str | None = None


@dataclass(frozen=True)
class ScreenTabSnapshot:
    """A tab inside a screen. Field order matters and is preserved."""

    id: str
    name: str
    fields: tuple[str, ...] = ()  # ordered field IDs as they appear on the tab


@dataclass(frozen=True)
class ScreenSnapshot:
    id: str
    name: str
    description: str = ""
    tabs: tuple[ScreenTabSnapshot, ...] = ()


@dataclass(frozen=True)
class ScreenSchemeSnapshot:
    """Maps Jira screen operations (default/create/edit/view) to screen IDs."""

    id: str
    name: str
    description: str = ""
    # operation -> screen id; operations: "default", "create", "edit", "view"
    mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IssueTypeScreenSchemeMappingSnapshot:
    issuetype_id: str  # numeric issuetype id, or "default" for the fallback
    screen_scheme_id: str


@dataclass(frozen=True)
class IssueTypeScreenSchemeSnapshot:
    id: str
    name: str
    description: str = ""
    mappings: tuple[IssueTypeScreenSchemeMappingSnapshot, ...] = ()


@dataclass(frozen=True)
class FieldConfigurationItemSnapshot:
    field_id: str
    required: bool = False
    hidden: bool = False
    description: str = ""


@dataclass(frozen=True)
class FieldConfigurationSnapshot:
    id: str
    name: str
    description: str = ""
    items: dict[str, FieldConfigurationItemSnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldConfigurationSchemeMappingSnapshot:
    issuetype_id: str
    field_configuration_id: str


@dataclass(frozen=True)
class FieldConfigurationSchemeSnapshot:
    id: str
    name: str
    description: str = ""
    mappings: tuple[FieldConfigurationSchemeMappingSnapshot, ...] = ()


@dataclass(frozen=True)
class IssueTypeSchemeSnapshot:
    """A Jira IssueTypeScheme: the set of issuetypes available within a
    project. Project ← IssueTypeScheme binding is reflected onto
    ``ProjectSnapshot.issuetype_scheme_id``.
    """

    id: str
    name: str
    description: str = ""
    issuetype_ids: tuple[str, ...] = ()  # ordered Jira issuetype IDs
    default_issuetype_id: str | None = None


@dataclass
class Snapshot:
    """Full reflected state of a Jira instance (subset stint manages)."""

    server_info: ServerInfoSnapshot
    custom_fields: dict[str, CustomFieldSnapshot] = field(default_factory=dict)
    issuetypes: dict[str, IssueTypeSnapshot] = field(default_factory=dict)
    projects: dict[str, ProjectSnapshot] = field(default_factory=dict)  # keyed by project key
    screens: dict[str, ScreenSnapshot] = field(default_factory=dict)
    screen_schemes: dict[str, ScreenSchemeSnapshot] = field(default_factory=dict)
    issuetype_schemes: dict[str, IssueTypeSchemeSnapshot] = field(default_factory=dict)
    issuetype_screen_schemes: dict[str, IssueTypeScreenSchemeSnapshot] = field(default_factory=dict)
    field_configurations: dict[str, FieldConfigurationSnapshot] = field(default_factory=dict)
    field_configuration_schemes: dict[str, FieldConfigurationSchemeSnapshot] = field(default_factory=dict)
