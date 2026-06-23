"""Engine: holds a dialect and a configured HTTP client.

Cloud-only as of 0.1. Explicit dialect via URL prefix (``jira_cloud+https://...``)
or the ``dialect=`` kwarg. Auto-detection without a hint is deferred because
it requires an async probe at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass

from stint.client.auth import Auth
from stint.client.http import JiraHTTPClient
from stint.dialects.base import Dialect
from stint.dialects.jira.cloud import JiraCloudDialect
from stint.exceptions import ConfigurationError
from stint.state.snapshot import Snapshot


@dataclass
class Engine:
    """Holds a configured client + dialect. Lifecycle: create -> use -> close."""

    base_url: str
    dialect: Dialect
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


_DIALECT_REGISTRY: dict[str, type[Dialect]] = {
    "jira_cloud": JiraCloudDialect,
}


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

    Raises ConfigurationError if the dialect is unknown or missing.
    """
    base_url, prefix_dialect = _split_dialect_prefix(url)
    chosen = dialect or prefix_dialect
    if not chosen:
        raise ConfigurationError(
            "create_engine requires a dialect. Use a URL prefix like "
            "'jira_cloud+https://...' or pass dialect='jira_cloud'."
        )
    if chosen not in _DIALECT_REGISTRY:
        raise ConfigurationError(f"Unknown dialect {chosen!r}. Known: {sorted(_DIALECT_REGISTRY)}")
    client = JiraHTTPClient(base_url, auth=auth, verify_ssl=verify_ssl, timeout=timeout)
    dialect_obj = _DIALECT_REGISTRY[chosen](client)
    return Engine(base_url=base_url, dialect=dialect_obj, client=client)


def _split_dialect_prefix(url: str) -> tuple[str, str | None]:
    """Split a URL of the form ``<dialect>+<scheme>://...`` into (rest, dialect).

    Returns (url, None) if no dialect prefix is present.
    """
    head, _, tail = url.partition("://")
    if not tail or "+" not in head:
        return url, None
    dialect, _, scheme = head.partition("+")
    return f"{scheme}://{tail}", dialect
