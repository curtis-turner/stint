"""stint - declarative schema-as-code and ORM for work-management systems.

Define projects, issuetypes, and custom fields as Pydantic models. Apply that
definition to a target Jira instance, mirror it across environments, then
insert and query issues through the same models. Jira is the first dialect.
The dialect protocol is the extension point for additional backends.

M0: schema-plane declarative classes (IssueType, Project, CustomField, Screen,
ScreenScheme, FieldConfiguration). Validation only, no I/O.

M1 first slice: dialect protocol, engine factory, HTTP client with PAT auth,
Jira DC dialect with /serverInfo detection and /field reflection, YAML state
file load/save.
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
    StintError,
    TransportError,
    UnsupportedTMPOpError,
)
from stint.fields import (
    CustomField,
    DateField,
    DateTimeField,
    MultiSelectField,
    NumberField,
    SelectField,
    TextAreaField,
    TextField,
    UserField,
)
from stint.migrations import Migration, MigrationContext, get_context, load_migrations, op
from stint.query import AsyncSession, CommitResult, Session, and_, not_, or_, select
from stint.registry import registry
from stint.schema import FieldConfiguration, IssueType, Project, Screen, ScreenScheme
from stint.state import (
    CustomFieldSnapshot,
    ServerInfoSnapshot,
    Snapshot,
    StateFile,
)
from stint.validate import validate, validate_or_raise

__version__ = "0.1.0a0"

__all__ = [
    "APITokenAuth",
    "AsyncSession",
    "AuthenticationError",
    "BasicAuth",
    "CommitResult",
    "ConfigurationError",
    "CustomField",
    "CustomFieldSnapshot",
    "DateField",
    "DateTimeField",
    "Engine",
    "FieldConfiguration",
    "IssueType",
    "Migration",
    "MigrationContext",
    "MultiSelectField",
    "NotFoundError",
    "NumberField",
    "PATAuth",
    "PartialCommitError",
    "StintError",
    "PermissionError",
    "Project",
    "ReflectionError",
    "RegistryError",
    "Screen",
    "ScreenScheme",
    "SelectField",
    "Session",
    "ServerInfoSnapshot",
    "Snapshot",
    "StateFile",
    "StateFileCorruptError",
    "StateFileError",
    "TextAreaField",
    "TextField",
    "TransportError",
    "UnsupportedTMPOpError",
    "UserField",
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
