"""stint error hierarchy."""


class StintError(Exception):
    """Base for all stint errors."""


class ConfigurationError(StintError):
    """Schema is internally inconsistent or references unknown values."""


class RegistryError(StintError):
    """A model is being registered twice or referenced before declaration."""


# ── Transport-level (HTTP) ────────────────────────────────────────────
class TransportError(StintError):
    """5xx, connection failure, or other unmapped HTTP error.

    ``status_code`` is the HTTP status when the error came from a response;
    ``None`` for connection-level failures with no response.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(StintError):
    """401 from the backend."""


class PermissionError(StintError):  # noqa: A001 - intentionally shadows builtin in stint namespace
    """403 from the backend. Often a Site Admin / Org Admin requirement."""


class NotFoundError(StintError):
    """404 from the backend."""


# ── State / reflection ────────────────────────────────────────────────
class StateFileError(StintError):
    """Base for state-file problems."""


class StateFileCorruptError(StateFileError):
    """State file exists but cannot be parsed."""


class ReflectionError(StintError):
    """Reflection produced unexpected or unreadable data."""


class UnsupportedTMPOpError(StintError):
    """The op cannot run because the target project is team-managed and the
    operation is one of the many TMP configuration steps that Atlassian does
    not expose via REST. The error message includes a Jira UI deep link so
    the user can complete the change there."""


# ── Data plane (M6 writes) ────────────────────────────────────────────
class PartialCommitError(StintError):
    """Raised by AsyncSession.commit when some operations succeeded and
    some failed. Jira admin REST has no transactions, so successful work is
    NOT rolled back. The exception carries the per-instance breakdown so the
    caller can decide what to do next.

    Use ``.results`` for the full list of CommitResult; ``.successes`` and
    ``.failures`` for filtered slices.
    """

    def __init__(self, results: list) -> None:
        self.results = results
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        msg = (
            f"commit partially failed: {len(successes)} succeeded, "
            f"{len(failures)} failed. First failure: "
            f"{failures[0].operation} on {failures[0].instance!r}: "
            f"{failures[0].error!r}"
        )
        super().__init__(msg)

    @property
    def successes(self) -> list:
        return [r for r in self.results if r.success]

    @property
    def failures(self) -> list:
        return [r for r in self.results if not r.success]
