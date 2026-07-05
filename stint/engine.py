"""Engine: holds a dialect and a configured HTTP client.

Cloud-only as of 0.1. Explicit dialect via URL prefix (``jira_cloud+https://...``)
or the ``dialect=`` kwarg. Auto-detection without a hint is deferred because
it requires an async probe at construction time.

``TmpEngine``/``create_tmp_engine`` are a deliberately separate pair, not a
``TmpDialect`` branch folded into ``Engine``/``create_engine``: ``TmpDialect``
does not satisfy ``BaseDialect`` (no ``detect``, and ``reflect`` takes a
required ``project_key`` -- see its module docstring), so a unioned
``Engine.dialect: CmpDialect | TmpDialect`` would make every existing
``CmpDialect``-only consumer (``stint.migrations.op``, ``stint.query.session``)
fail to type-check against a method TmpDialect doesn't have. Two small,
separately-typed engines avoid that collapse while still sharing the same
URL-prefix selection UX.

Referencing ``TmpDialect`` here is the one sanctioned exception to the
isolation rule in ``tmp_dialect_design.md`` ("nothing outside
stint/dialects/jira/tmp/ imports from inside it except the dialect registry
at selection time"): this module *is* that registry. Taken literally, "at
selection time" means the import itself must not happen any earlier than
that -- so it is deferred to inside ``create_tmp_engine``, not hoisted to
module level. ``import stint`` (which imports this module) must never import
the TMP package for a CMP-only user; a real, module-level import here would
do exactly that, for every user, regardless of which dialect they select.
The module-level references below (``TmpEngine.dialect``, ``create_tmp_engine``'s
return type) are resolved only under ``TYPE_CHECKING``, which type checkers
honor but Python never executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stint.client.auth import Auth
from stint.client.http import JiraHTTPClient
from stint.dialects.base import CmpDialect
from stint.dialects.jira.cloud import JiraCloudDialect
from stint.exceptions import ConfigurationError
from stint.state.snapshot import Snapshot

if TYPE_CHECKING:
    from stint.dialects.jira.tmp.dialect import TmpDialect
    from stint.dialects.jira.tmp.models import TmpSnapshot


@dataclass
class Engine:
    """Holds a configured client + dialect. Lifecycle: create -> use -> close."""

    base_url: str
    dialect: CmpDialect
    client: JiraHTTPClient

    async def reflect(self) -> Snapshot:
        return await self.dialect.reflect()

    async def detect(self) -> bool:
        """Confirm the configured dialect matches the live server."""
        return await self.dialect.detect()

    async def close(self) -> None:
        await self.client.close()

    async def __aenter__(self) -> Engine:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


@dataclass
class TmpEngine:
    """Holds a configured client + ``TmpDialect``. See module docstring for
    why this is a separate class from ``Engine`` rather than a shared one."""

    base_url: str
    dialect: TmpDialect
    client: JiraHTTPClient

    async def reflect(self, *, project_key: str) -> TmpSnapshot:
        return await self.dialect.reflect(project_key=project_key)

    async def close(self) -> None:
        await self.client.close()

    async def __aenter__(self) -> TmpEngine:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


_CMP_DIALECT_REGISTRY: dict[str, type[CmpDialect]] = {
    "jira_cloud": JiraCloudDialect,
}

_TMP_DIALECT_NAME = "jira_cloud_tmp"

# All known dialect names, CMP and TMP together -- used only to give a
# correct "unknown dialect" vs. "wrong constructor" error message.
_ALL_DIALECT_NAMES = frozenset({*_CMP_DIALECT_REGISTRY, _TMP_DIALECT_NAME})


def resolve_dialect_name(url: str, dialect: str | None) -> tuple[str, str]:
    """Split the URL and settle on a dialect name from the prefix/kwarg.

    Public so callers that must pick between ``create_engine`` and
    ``create_tmp_engine`` (see ``stint.cli.cmd_reflect``) can resolve the
    dialect name once, before constructing either.

    Raises ConfigurationError if no dialect is given, or if the name is not
    recognized at all (a name recognized by only the other constructor gets
    a more specific error from the caller).
    """
    base_url, prefix_dialect = _split_dialect_prefix(url)
    chosen = dialect or prefix_dialect
    if not chosen:
        raise ConfigurationError(
            "no dialect given. Use a URL prefix like 'jira_cloud+https://...' "
            "(or 'jira_cloud_tmp+https://...') or pass dialect='jira_cloud'."
        )
    if chosen not in _ALL_DIALECT_NAMES:
        raise ConfigurationError(f"Unknown dialect {chosen!r}. Known: {sorted(_ALL_DIALECT_NAMES)}")
    return base_url, chosen


def create_engine(
    url: str,
    *,
    auth: Auth,
    dialect: str | None = None,
    verify_ssl: bool = True,
    timeout: float = 30.0,
) -> Engine:
    """Build an Engine from a URL plus auth.

    URL forms:
      - ``jira_cloud+https://you.atlassian.net`` - explicit dialect prefix
      - ``https://you.atlassian.net`` with ``dialect="jira_cloud"`` kwarg

    Raises ConfigurationError if the dialect is unknown or missing. For the
    experimental team-managed project dialect (``jira_cloud_tmp``), use
    ``create_tmp_engine`` instead -- it does not fit this ``Engine`` shape.
    """
    base_url, chosen = resolve_dialect_name(url, dialect)
    if chosen not in _CMP_DIALECT_REGISTRY:
        raise ConfigurationError(f"create_engine {chosen!r}: use create_tmp_engine() for '{_TMP_DIALECT_NAME}'.")
    client = JiraHTTPClient(base_url, auth=auth, verify_ssl=verify_ssl, timeout=timeout)
    dialect_obj = _CMP_DIALECT_REGISTRY[chosen](client)
    return Engine(base_url=base_url, dialect=dialect_obj, client=client)


def create_tmp_engine(
    url: str,
    *,
    auth: Auth,
    dialect: str | None = None,
    verify_ssl: bool = True,
    timeout: float = 30.0,
) -> TmpEngine:
    """Build a TmpEngine (the experimental team-managed project dialect).

    URL forms mirror ``create_engine``: ``jira_cloud_tmp+https://...`` prefix,
    or ``dialect="jira_cloud_tmp"``. Raises ConfigurationError if the resolved
    dialect is anything other than ``jira_cloud_tmp``.
    """
    base_url, chosen = resolve_dialect_name(url, dialect)
    if chosen != _TMP_DIALECT_NAME:
        raise ConfigurationError(f"create_tmp_engine only supports {_TMP_DIALECT_NAME!r}, got {chosen!r}.")
    from stint.dialects.jira.tmp.dialect import TmpDialect  # deferred: see module docstring

    client = JiraHTTPClient(base_url, auth=auth, verify_ssl=verify_ssl, timeout=timeout)
    return TmpEngine(base_url=base_url, dialect=TmpDialect(client), client=client)


def _split_dialect_prefix(url: str) -> tuple[str, str | None]:
    """Split a URL of the form ``<dialect>+<scheme>://...`` into (rest, dialect).

    Returns (url, None) if no dialect prefix is present.
    """
    head, _, tail = url.partition("://")
    if not tail or "+" not in head:
        return url, None
    dialect, _, scheme = head.partition("+")
    return f"{scheme}://{tail}", dialect
