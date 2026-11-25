"""stint - declarative schema-as-code and ORM for work-management systems.

Define projects, issuetypes, and custom fields as Pydantic models. Apply that
definition to a target Jira instance, mirror it across environments, then
insert and query issues through the same models. Jira is the first dialect.
The dialect protocol is the extension point for additional backends.

M0: schema-plane declarative classes (IssueType, Project, CustomField, Screen,
ScreenScheme, FieldConfiguration). Validation only, no I/O.

M1 first slice: dialect protocol, engine factory, HTTP client with API-token
auth, Jira Cloud dialect with /serverInfo detection and /field reflection,
YAML state file load/save.
"""

from stint import migrations
from stint.audit import ControlFieldAuditReport, ControlFieldWrite
from stint.client.auth import APITokenAuth, BasicAuth, PATAuth
from stint.engine import Engine, create_engine
from stint.exceptions import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    PartialCommitError,
    PermissionError,
    ReflectionError,
    RegistryError,
    StateFileCorruptError,
    StateFileError,
    StateFileWriteError,
    StintError,
    TransitionError,
    TransportError,
    UnsupportedTMPOpError,
)
from stint.fields import (
    CheckboxesField,
    CustomField,
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
from stint.migrations import Migration, MigrationContext, get_context, load_migrations, op
from stint.query import AsyncSession, CommitResult, Session, and_, not_, or_, select
from stint.registry import registry
from stint.schema import FieldConfiguration, IssueType, Project, Screen, ScreenScheme
from stint.schema.automation import AutomationRule, AutomationRuleScheme, ControlFieldGuard
from stint.schema.permissions import (
    ApplicationRole,
    Assignee,
    PermissionScheme,
    ProjectLead,
    ProjectRole,
    Reporter,
)
from stint.schema.roles import Group, User
from stint.schema.security import SecurityLevel, SecurityLevelMember, SecurityScheme
from stint.schema.workflows import Permission, Status, Transition, Workflow, WorkflowScheme
from stint.state import (
    CustomFieldSnapshot,
    SecurityLevelMemberSnapshot,
    SecurityLevelSnapshot,
    SecuritySchemeSnapshot,
    ServerInfoSnapshot,
    Snapshot,
    StateFile,
)
from stint.validate import validate, validate_or_raise

__version__ = "0.21.0"

__all__ = [
    "APITokenAuth",
    "ApplicationRole",
    "Assignee",
    "AsyncSession",
    "AuthenticationError",
    "AutomationRule",
    "AutomationRuleScheme",
    "BasicAuth",
    "CheckboxesField",
    "CommitResult",
    "ConfigurationError",
    "ControlFieldAuditReport",
    "ControlFieldGuard",
    "ControlFieldWrite",
    "CustomField",
    "CustomFieldSnapshot",
    "DateField",
    "DateTimeField",
    "Engine",
    "FieldConfiguration",
    "Group",
    "GroupField",
    "IssueType",
    "LabelsField",
    "Migration",
    "MigrationContext",
    "MultiGroupField",
    "MultiSelectField",
    "MultiUserField",
    "MultiVersionField",
    "NotFoundError",
    "NumberField",
    "PATAuth",
    "PartialCommitError",
    "PermissionScheme",
    "ProjectLead",
    "ProjectRole",
    "Reporter",
    "StintError",
    "PermissionError",
    "Permission",
    "Project",
    "RadioButtonsField",
    "ReadOnlyField",
    "ReflectionError",
    "RegistryError",
    "Screen",
    "ScreenScheme",
    "SecurityLevel",
    "SecurityLevelMember",
    "SecurityLevelMemberSnapshot",
    "SecurityLevelSnapshot",
    "SecurityScheme",
    "SecuritySchemeSnapshot",
    "SelectField",
    "Session",
    "ServerInfoSnapshot",
    "Snapshot",
    "StateFile",
    "StateFileCorruptError",
    "StateFileError",
    "StateFileWriteError",
    "Status",
    "TextAreaField",
    "TextField",
    "Transition",
    "TransitionError",
    "TransportError",
    "URLField",
    "UnsupportedTMPOpError",
    "User",
    "UserField",
    "VersionField",
    "Workflow",
    "WorkflowScheme",
    "and_",
    "create_engine",
    "get_context",
    "load_migrations",
    "migrations",
    "not_",
    "op",
    "or_",
    "registry",
    "select",
    "validate",
    "validate_or_raise",
]


# Issue Security Scheme Declarations
# ``SecurityLevel``/``SecurityLevelMember``/``SecurityScheme`` now come from
# the schema plane (``stint.schema.security``); read your declaration data there.
# The snapshot types (suffixed "Snapshot") stay in ``stint.state`` and represent
# reflected state from Jira.
