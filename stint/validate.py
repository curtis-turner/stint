"""Schema-level validation. Catches inconsistencies a declarative author can miss.

For M0 spike: validates the global registry. Later milestones will accept a
specific module path (`stint validate schemas/platform.py`) and validate just
that module's registrations.
"""

from __future__ import annotations

from stint.exceptions import ConfigurationError
from stint.fields import CustomField
from stint.registry import registry


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

    # 6. Every Project's __security_scheme__ is a registered declaration.
    for key, project_cls in registry.projects.items():
        ss = getattr(project_cls, "__security_scheme__", None)
        if ss is not None and getattr(ss, "alias", "") not in registry.security_schemes:
            problems.append(
                f"Project {key!r} references SecurityScheme {getattr(ss, 'alias', None)!r} which is not registered."
            )
        # 7. A scheme-side ``project=`` hint must name this project (or be
        # unset). A stale hint fails only the diff warning path — surface it
        # here too, where it's cheaper to fix.
        if ss is not None and getattr(ss, "project", None) not in (None, key):
            problems.append(
                f"Project {key!r} references SecurityScheme {ss.alias!r} whose "
                f"project hint is {ss.project!r}; expected None or {key!r}."
            )

        # 8. Every Project's __permission_scheme__ is a registered
        # declaration; a scheme-side ``project=`` hint must name this
        # project (or be unset) — same rules as the security scheme.
        ps = getattr(project_cls, "__permission_scheme__", None)
        if ps is not None and getattr(ps, "alias", "") not in registry.permission_schemes:
            problems.append(
                f"Project {key!r} references PermissionScheme {getattr(ps, 'alias', None)!r} which is not registered."
            )
        if ps is not None and getattr(ps, "project", None) not in (None, key):
            problems.append(
                f"Project {key!r} references PermissionScheme {ps.alias!r} whose "
                f"project hint is {ps.project!r}; expected None or {key!r}."
            )

        # 9. A Project's __permission_scheme__ may reference ProjectRole
        # holders by name; when the schema also declares those roles via
        # __roles__, the names must match exactly (case-insensitive), so a
        # typo'd holder does not silently grant to a different role.
        roles = getattr(project_cls, "__roles__", None) or {}
        if ps is not None and roles:
            declared_role_names = {name.lower() for name in roles}
            for permission, holders in (getattr(ps, "grants", {}) or {}).items():
                for holder in holders:
                    if type(holder).__name__ == "ProjectRole" and holder.name.lower() not in declared_role_names:
                        problems.append(
                            f"Project {key!r}: permission scheme {ps.alias!r} grants "
                            f"{permission} to ProjectRole {holder.name!r}, which the "
                            f"schema does not declare in __roles__; the role must exist "
                            f"in Jira (or be declared here) for the grant to hold anyone."
                        )

    # 10. Unknown permission keys in declared PermissionSchemes are already
    # rejected at declaration time (PermissionScheme.__post_init__ re-raises
    # RegistryError, which `stint validate` surfaces via the module import);
    # this loop catches schemes constructed with a mutated registry for
    # tests, and doubles as the documented "stint validate rejects unknown
    # permission keys" rule.
    from stint.schema.permissions import KNOWN_PERMISSION_KEYS

    for alias, scheme in registry.permission_schemes.items():
        for permission in getattr(scheme, "grants", {}) or {}:
            if permission not in KNOWN_PERMISSION_KEYS:
                problems.append(f"PermissionScheme {alias!r} grants unknown permission key {permission!r}.")

    # 11. Workflow restrict holders: a declared ProjectRole restrict must be
    # a role the schema declares in some project's __roles__ (same
    # case-insensitive cross-check as permission-scheme grants; project
    # roles are tenant-global), so a typo'd restrict does not silently
    # hand the transition to a different role.
    declared_role_names = {
        name.lower()
        for project_cls in registry.projects.values()
        for name in (getattr(project_cls, "__roles__", None) or {})
    }
    if declared_role_names:
        for wf_alias, workflow in registry.workflows.items():
            for transition in getattr(workflow, "transitions", ()) or ():
                for holder in getattr(transition, "restrict_to", ()) or ():
                    if type(holder).__name__ == "ProjectRole" and holder.name.lower() not in declared_role_names:
                        problems.append(
                            f"Workflow {wf_alias!r} transition {transition.name!r} restricts to "
                            f"ProjectRole {holder.name!r}, which the schema does not declare "
                            f"in any project's __roles__; the role must exist in Jira (or be "
                            f"declared here) for the restrict to hold anyone."
                        )

    # 12. Automation rule schemes: registered bindings, guard allow-lists
    # cross-checked against __roles__, and guard field references
    # resolvable to declared custom fields.
    for scheme_alias, scheme in registry.automation_rule_schemes.items():
        for rule in getattr(scheme, "rules", ()) or ():
            if getattr(rule, "alias", "") not in registry.automation_rules:
                problems.append(
                    f"AutomationRuleScheme {scheme_alias!r} references rule {getattr(rule, 'alias', None)!r} "
                    f"which is not registered."
                )
        if getattr(scheme, "project", None) is not None and scheme.project not in registry.projects:
            problems.append(
                f"AutomationRuleScheme {scheme_alias!r} has project hint {scheme.project!r}, "
                f"which is not a declared Project key."
            )
    for guard_alias, guard in registry.automation_rules.items():
        if not getattr(guard, "is_guard", False):
            continue
        for _name, kind, declared in guard.writer_specs():
            if kind == "role" and declared.lower() not in declared_role_names:
                problems.append(
                    f"ControlFieldGuard {guard_alias!r} allows ProjectRole {declared!r}, which "
                    f"the schema does not declare in any project's __roles__; the role must "
                    f"exist in Jira (or be declared here) for the allow-list to hold anyone."
                )
        if guard.fields is not None:
            for field_ref in guard.fields:
                if isinstance(field_ref, CustomField) and field_ref.alias not in registry.custom_fields:
                    problems.append(
                        f"ControlFieldGuard {guard_alias!r} guards CustomField {field_ref.alias!r} "
                        f"which is not registered."
                    )

    return problems


def validate_or_raise() -> None:
    """Run `validate()` and raise `ConfigurationError` if any problems found."""
    problems = validate()
    if problems:
        raise ConfigurationError("Schema validation failed:\n" + "\n".join(f"  - {p}" for p in problems))
