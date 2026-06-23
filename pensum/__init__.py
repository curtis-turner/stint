"""pensum - declarative schema-as-code and ORM for work-management systems.

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

from pensum import migrations
from pensum.client.auth import APITokenAuth, BasicAuth, PATAuth
from pensum.engine import Engine, create_engine
from pensum.exceptions import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    PartialCommitError,
    PensumError,
    PermissionError,
    ReflectionError,
    RegistryError,
    StateFileCorruptError,
    StateFileError,
    TransportError,
    UnsupportedTMPOpError,
)
from pensum.fields import (
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
from pensum.migrations import Migration, MigrationContext, get_context, load_migrations, op
from pensum.query import AsyncSession, CommitResult, Session, and_, not_, or_, select
from pensum.registry import registry
from pensum.schema import FieldConfiguration, IssueType, Project, Screen, ScreenScheme
from pensum.state import (
    CustomFieldSnapshot,
    ServerInfoSnapshot,
    Snapshot,
    StateFile,
)
from pensum.validate import validate, validate_or_raise

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
    "PensumError",
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
