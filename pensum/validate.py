"""Schema-level validation. Catches inconsistencies a declarative author can miss.

For M0 spike: validates the global registry. Later milestones will accept a
specific module path (`pensum validate schemas/platform.py`) and validate just
that module's registrations.
"""

from __future__ import annotations

from pensum.exceptions import ConfigurationError
from pensum.registry import registry


def validate() -> list[str]:
    """Return a list of human-readable problems. Empty means schema is valid.

    Does not raise. Lets callers (CLI, tests) decide what to do with the list.
    """
    problems: list[str] = []

    # 1. Every IssueType referenced by a Project actually exists in the registry.
    for project_cls in registry.projects.values():
        for it in getattr(project_cls, "__issuetypes__", ()):
            alias = getattr(it, "__alias__", None)
            if not alias or alias not in registry.issuetypes:
                problems.append(
                    f"Project {project_cls.__name__!r} references issuetype {it.__name__!r} which is not registered."
                )

    # 2. Every CustomField referenced by a Screen exists in the registry.
    for screen in registry.screens.values():
        for ref in screen.fields:
            if isinstance(ref, str):
                continue  # system field, accept by convention
            if ref.alias not in registry.custom_fields:
                problems.append(
                    f"Screen {screen.alias!r} references CustomField {ref.alias!r} which is not registered."
                )

    # 3. Every Screen referenced by a ScreenScheme exists in the registry.
    for scheme in registry.screen_schemes.values():
        for slot_name in ("create", "edit", "view"):
            screen = getattr(scheme, slot_name)
            if screen.alias not in registry.screens:
                problems.append(
                    f"ScreenScheme {scheme.alias!r}.{slot_name} references "
                    f"Screen {screen.alias!r} which is not registered."
                )

    # 4. Every IssueType's __screen_scheme__ is registered.
    for it_cls in registry.issuetypes.values():
        ss = getattr(it_cls, "__screen_scheme__", None)
        if ss is not None and ss.alias not in registry.screen_schemes:
            problems.append(
                f"IssueType {it_cls.__name__!r} references ScreenScheme {ss.alias!r} which is not registered."
            )

    # 5. Every IssueType's __field_configuration__ is registered.
    for it_cls in registry.issuetypes.values():
        fc = getattr(it_cls, "__field_configuration__", None)
        if fc is not None and fc.alias not in registry.field_configurations:
            problems.append(
                f"IssueType {it_cls.__name__!r} references FieldConfiguration {fc.alias!r} which is not registered."
            )

    return problems


def validate_or_raise() -> None:
    """Run `validate()` and raise `ConfigurationError` if any problems found."""
    problems = validate()
    if problems:
        raise ConfigurationError("Schema validation failed:\n" + "\n".join(f"  - {p}" for p in problems))
