"""Schema-plane declarations for project role actors (``Project.__roles__``).

``User`` and ``Group`` are lightweight value types a schema author writes in a
``Project.__roles__`` mapping:

```python
class Tracker(Project):
    __roles__ = {
        "Administrators": [User("svc-automation@example.com")],
        "Viewers":        [Group("all-staff")],
    }
```

They double as permission-scheme grant holders (``stint.schema.permissions``):
a grant list may mix ``ProjectRole`` with ``User``/``Group``. Emails resolve
to accountIds at apply time via the same user-search API the project lead and
data-plane user fields use; group names pass through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    """A user actor/holder: an email address or a raw Jira accountId.

    Emails resolve to accountIds at apply time (``resolve_user``); a value
    without ``@`` is assumed already-resolved and passes through unchanged,
    matching the lead/data-plane convention.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("User requires a non-empty email or accountId")

    @property
    def is_email(self) -> bool:
        return "@" in self.value


@dataclass(frozen=True)
class Group:
    """A group actor/holder: a Jira group name."""

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Group requires a non-empty group name")


Actor = User | Group
