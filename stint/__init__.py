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
from stint.schema.security import SecurityLevel, SecurityLevelMember, SecurityScheme
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

__version__ = "0.7.1"

__all__ = [
    "APITokenAuth",
    "AsyncSession",
    "AuthenticationError",
    "BasicAuth",
    "CheckboxesField",
    "CommitResult",
    "ConfigurationError",
    "CustomField",
    "CustomFieldSnapshot",
    "DateField",
    "DateTimeField",
    "Engine",
    "FieldConfiguration",
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
    "StintError",
    "PermissionError",
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
    "TextAreaField",
    "TextField",
    "TransportError",
    "URLField",
    "UnsupportedTMPOpError",
    "UserField",
    "VersionField",
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
