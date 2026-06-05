"""Engine: holds a dialect and a configured HTTP client.

M1 first slice: explicit dialect via URL prefix (``jira_dc+https://...``)
or via the ``dialect=`` kwarg. Auto-detection without a hint is deferred
because it requires an async probe at construction time; the factory would
have to be async (`create_engine_async`) and that is a bigger API decision
worth a separate round.
"""

from __future__ import annotations

from dataclasses import dataclass

from pensum.client.auth import Auth
from pensum.client.http import JiraHTTPClient
from pensum.dialects.base import Dialect
from pensum.dialects.jira.cloud import JiraCloudDialect
from pensum.dialects.jira.dc import JiraDCDialect
from pensum.exceptions import ConfigurationError
from pensum.state.snapshot import Snapshot


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
    "jira_dc": JiraDCDialect,
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
      - ``jira_dc+https://jira.example.com`` - explicit dialect prefix
      - ``https://jira.example.com`` with ``dialect="jira_dc"`` kwarg

    Raises ConfigurationError if the dialect is unknown or missing.
    """
    base_url, prefix_dialect = _split_dialect_prefix(url)
    chosen = dialect or prefix_dialect
    if not chosen:
        raise ConfigurationError(
            "create_engine requires a dialect. Use a URL prefix like 'jira_dc+https://...' or pass dialect='jira_dc'."
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
