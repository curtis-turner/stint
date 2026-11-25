"""Schema-plane declarations for Jira Automation rules (``AutomationRule``,
``ControlFieldGuard``) and rule schemes (``AutomationRuleScheme``).

Automation rules are tenant objects scoped to one or more projects, exactly
like permission and workflow schemes: declared, reflected, diffed into ops,
applied, and recorded in state by uuid. A project binds one scheme with
``Project.__automation_rule_scheme__``; every rule in the scheme is scoped to
the bound projects (one multi-project scope, never copies).

Two declarations:

- ``AutomationRule``: a rule whose payload comes from a JSON file — the
  export from the Automation UI (``GET rule/{uuid}``), with tenant ids
  replaced by stint references. ``refs`` maps reference names to declared
  objects; the payload addresses them as ``{{stint.<kind>.<name>}}``
  placeholders that stint substitutes at apply time (custom fields from
  state, role/group ids, user accountIds) and fails validation on an
  unknown reference.

- ``ControlFieldGuard``: the revert rule generated from the schema. The
  detect-and-revert control for the restricted-Edit-screen pattern: the
  screen lock is cosmetic for API clients (JRACLOUD-98054), so a guard rule
  fires on "Field value changed" for the control fields and puts the old
  value back when the writer is not an allowed writer. stint knows the
  control fields (create/view screens minus the edit screen), the field
  types, and the identities that should be allowed — the payload is
  generated from those, with the same ``{{stint.*}}`` placeholders.

Cross-cutting rule: **never let the guard fight the service.** The service
account (the authenticated account) and the rule's actor are always allowed
writers, enforced by ``stint validate`` and at apply time.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from stint.exceptions import ConfigurationError, RegistryError
from stint.fields import (
    CheckboxesField,
    CustomField,
    GroupField,
    LabelsField,
    MultiGroupField,
    MultiSelectField,
    MultiUserField,
    MultiVersionField,
    UserField,
    jira_system_field_id,
)
from stint.registry import _declaring_module, registry
from stint.schema.roles import Group, User

__all__ = [
    "AutomationRule",
    "AutomationRuleScheme",
    "ControlFieldGuard",
    "build_guard_payload",
    "guard_restore_value",
]

# Kinds of stint reference a payload placeholder can address. The payload
# spells them ``{{stint.<kind>.<name>}}``; ``refs`` maps ``name`` to the
# declared object the placeholder resolves to.
REF_KINDS: frozenset[str] = frozenset({"field", "system", "role", "group", "user"})

# Operation filter on the Field value changed trigger: guard rules only fire
# for edits. Create stays allowed (the create screen is the authoring
# surface), and transition-screen changes are optional and left off by
# default.
_GUARD_TRIGGER_OPERATIONS: list[str] = ["edit"]

# Automation component types, pinned against Atlassian KB rule exports
# (see notes/automation_api_research.md). Kept as constants so the generator
# and the tests can never disagree.
TRIGGER_FIELD_CHANGED = "jira.issue.field.changed"
CONDITION_USER = "jira.user.condition"
CONDITION_IF_BLOCK = "jira.condition.if.block"
CONDITION_CONTAINER_BLOCK = "jira.condition.container.block"
ACTION_EDIT_ISSUE = "jira.issue.edit"
ACTION_COMMENT = "jira.issue.comment"

# jira.user.condition check names. USER_IS_NOT and NOT_IN_GROUP are confirmed
# from rule exports; the project-role spelling is by symmetry and is the one
# value a live pass must confirm first (see notes/automation_api_research.md).
CHECK_USER_IS_NOT = "USER_IS_NOT"
CHECK_NOT_IN_GROUP = "NOT_IN_GROUP"
CHECK_NOT_IN_PROJECT_ROLE = "NOT_IN_PROJECT_ROLE"


def ref_placeholder(kind: str, name: str) -> str:
    """The payload placeholder for one ref: ``{{stint.<kind>.<name>}}``."""
    if kind not in REF_KINDS:
        raise RegistryError(f"unknown stint ref kind {kind!r}; expected one of {sorted(REF_KINDS)}")
    return f"{{{{stint.{kind}.{name}}}}}"


def substitute_refs(obj: Any, resolved: dict[str, str]) -> Any:
    """Recursively replace ``{{stint.*}}`` placeholders with resolved values.

    Applies to dict keys too (a guard's advancedFields JSON restores by
    ``{{stint.field.<alias>}}`` as the field key). Placeholders nested
    inside generated smart values (``{{#changelog.{{stint.field.x}}}}``)
    resolve because each placeholder is matched literally, wherever it
    appears. Unknown placeholders are left intact — ``unresolved_refs``
    reports them so ops can fail loudly instead of writing a rule Jira
    cannot evaluate.
    """
    if isinstance(obj, str):
        out = obj
        for placeholder, value in resolved.items():
            if placeholder in out:
                out = out.replace(placeholder, value)
        return out
    if isinstance(obj, dict):
        return {substitute_refs(k, resolved): substitute_refs(v, resolved) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_refs(item, resolved) for item in obj]
    return obj


def unresolved_refs(obj: Any) -> list[str]:
    """Every ``{{stint.*}}`` placeholder still present after substitution."""
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            found.extend(m.group(0) for m in _PLACEHOLDER_RE.finditer(node))
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(k)
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return sorted(set(found))


# Placeholder finder shared by substitution validation. The ref name is any
# run of non-brace characters (emails and aliases carry dots/@ signs).
_PLACEHOLDER_RE = re.compile(r"\{\{stint\.[a-z]+\.[^{}]+?\}\}")


# Keys the diff normalization drops from both sides: server-generated ids,
# timestamps, billing/rule metadata, and fields compared through their own
# endpoints (state, scope). Component/trigger ids are canonicalized
# separately (positions), not dropped — parent links reference them.
_SERVER_NOISE_KEYS = frozenset(
    {"created", "updated", "uuid", "id", "tags", "ruleHome", "billingType", "authorAccountId", "collaborators"}
)
_PAYLOAD_LEVEL_KEYS = _SERVER_NOISE_KEYS | {"state", "ruleScopeARIs", "projects"}


def normalized_rule_payload(payload: dict[str, Any], *, is_guard: bool) -> dict[str, Any]:
    """A canonical form of a rule payload for idempotent diffs.

    Drops server-generated noise (ids, timestamps, tags, rule metadata) and
    everything managed by its own op (state, ruleScopeARIs). The
    component/trigger tree is canonicalized: each component's ``id`` is
    replaced by its position and parent links are remapped through it, so a
    declared payload (id-less) and a reflected one (server ids) compare
    equal when the structure and values match. For guards the ``actor`` is
    dropped too — stint omits it (Jira's "Automation for Jira" default),
    while reflection reports the tenant's resolved actor id.
    """
    import copy

    body = {k: copy.deepcopy(v) for k, v in payload.items() if k not in _PAYLOAD_LEVEL_KEYS}
    if is_guard:
        body.pop("actor", None)

    def _canonical(node: Any, ids: dict[Any, Any]) -> Any:
        if isinstance(node, list):
            return [_canonical(item, ids) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "id" and not isinstance(value, dict):
                continue  # positional canonicalization: ids map to list order
            if key in ("parentId", "conditionParentId"):
                # A null parent (top-level component) stays null; a real id
                # maps to the parent's position.
                out[key] = ids.get(value, value) if value is not None else None
                continue
            out[key] = _canonical(value, ids)
        return out

    def _canonical_components(components: list[Any]) -> list[Any]:
        id_map: dict[Any, Any] = {
            c.get("id"): i for i, c in enumerate(components) if isinstance(c, dict) and c.get("id") is not None
        }
        return [_canonical(c, id_map) for c in components]

    trigger = body.get("trigger")
    if isinstance(trigger, dict):
        body["trigger"] = _canonical(trigger, {})
    components = body.get("components")
    if isinstance(components, list):
        body["components"] = _canonical_components(components)
    return body


def _project_roles(writers: list[Any]) -> list[Any]:
    """The ProjectRole instances in a writers list (narrowed for the checker)."""
    from stint.schema.permissions import ProjectRole

    return [w for w in writers if isinstance(w, ProjectRole)]


def _ref_spec(value: Any) -> tuple[str, str]:
    """A refs value -> (kind, declared value) pair.

    CustomField references resolve to ``customfield_NNNNN`` from state;
    system field names normalize via ``jira_system_field_id`` ("Priority" ->
    "priority"); roles/groups resolve to ids and users to accountIds at
    apply time, like everywhere else stint handles them.
    """
    if isinstance(value, CustomField):
        return "field", value.alias
    if isinstance(value, str):
        return "system", jira_system_field_id(value)
    if type(value).__name__ == "ProjectRole":
        return "role", value.name
    if isinstance(value, Group):
        return "group", value.name
    if isinstance(value, User):
        return "user", value.value
    raise RegistryError(
        f"unsupported refs value {value!r}; use a CustomField, a system field name, ProjectRole, Group, or User"
    )


def _resolve_payload(payload: Path | str, *, alias: str) -> Path:
    """Resolve a payload path: declaring-module directory first, then CWD.

    Schema files live in their own directory with their payload files
    beside them; relative paths in a declaration mean relative to that
    file, so applying does not depend on where stint runs from. Interactive
    (``__main__``) declarations and tests fall through to CWD as before.
    """
    p = Path(payload)
    if p.is_absolute() or p.exists():
        return p
    module = _declaring_module()
    if module:
        module_file = getattr(sys.modules.get(module), "__file__", None)
        if module_file:
            candidate = Path(module_file).parent / p
            if candidate.exists():
                return candidate
    return p


def _load_payload(payload: Path | str, *, alias: str) -> dict[str, Any]:
    path = _resolve_payload(payload, alias=alias)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigurationError(
            f"AutomationRule {alias!r}: cannot read payload {path}: {e}. "
            f"Relative paths resolve against the directory of the file that "
            f"declares the rule, then the working directory."
        ) from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigurationError(f"AutomationRule {alias!r}: payload {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"AutomationRule {alias!r}: payload {path} must be a JSON object (the rule payload), "
            f"got {type(data).__name__}"
        )
    return data


@dataclass
class AutomationRule:
    """One Jira Automation rule, declared from a payload file.

    Registering fields:

    - ``alias``: stable identifier across tenants (state-file key). The
      rule's uuid is recorded in state at apply time.
    - ``name``: Jira display name; the handle rules adopt by on first
      application. Overrides the payload's ``name``.
    - ``payload``: path to the rule JSON (the ``GET rule/{uuid}`` export
      shape, tenant ids replaced by ``{{stint.*}}`` references). Loaded at
      declaration; the rendered migration carries the payload inline, so
      applying never needs the file again. Relative paths resolve against
      the declaring schema file.
    - ``refs``: reference name -> declared object (CustomField, system
      field name, ProjectRole, Group, User). Every ``{{stint.<kind>.<name>}}``
      placeholder in the payload resolves through this mapping; an unknown
      name fails validation.
    - ``enabled``: the rule's run state; applied via the state endpoint,
      overriding the payload's ``state``.

    The payload is the source of truth for everything else (trigger,
    conditions, actions, actor, labels). The declaration's ``name`` and
    ``enabled`` win over the payload's.
    """

    alias: str
    name: str = ""
    description: str = ""
    payload: Path | str = ""
    refs: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    # Declaring module for scoped autogenerate; recorded by the registry.
    _declared_module: str = field(default="", repr=False, compare=False)
    # Loaded rule JSON (placeholders intact), filled by __post_init__.
    payload_data: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    is_guard: ClassVar[bool] = False

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("AutomationRule declaration requires a non-empty alias")
        if not self.name:
            self.name = self.alias
        if not self.payload:
            raise RegistryError(
                f"AutomationRule {self.alias!r}: payload path is required "
                f"(the rule JSON exported from the Automation UI, with stint refs in place of tenant ids)"
            )
        for ref_name, value in self.refs.items():
            if not ref_name or not str(ref_name).strip():
                raise RegistryError(f"AutomationRule {self.alias!r}: ref names must be non-empty")
            _ref_spec(value)  # validates the value type early
        self.payload_data = _load_payload(self.payload, alias=self.alias)
        module = _declaring_module()
        self._declared_module = module
        registry.register_automation_rule(self)

    def ref_specs(self) -> list[tuple[str, str, str]]:
        """refs as (name, kind, declared value) triples, declaration order."""
        out: list[tuple[str, str, str]] = []
        for ref_name, value in self.refs.items():
            kind, declared = _ref_spec(value)
            out.append((str(ref_name), kind, declared))
        return out


@dataclass
class ControlFieldGuard:
    """The detect-and-revert rule for control fields, generated from the schema.

    A guard rule fires on "Field value changed" for the guarded fields
    (default: the control fields derived from the bound project's declared
    screens — create/view minus edit), checks who made the change, and puts
    the previous value back when the writer is not an allowed writer.

    - ``fields``: explicit field list (CustomField instances or system field
      names) to add or narrow the derivation. ``None`` derives the control
      fields from the bound project at autogenerate time.
    - ``allowed_writers``: User emails, Group names, and ProjectRole names
      whose edits pass through. The authenticated service account and the
      rule actor are always allowed, enforced by ``stint validate`` and at
      apply time — a guard that reverts the service's own writes is worse
      than no guard.
    - ``on_violation``: ``"revert"`` (restore the previous values,
      notifications off so watchers are not paged twice) and ``"comment"``
      (note the revert on the issue, mentioning the initiator).
      ``"notify"``/``"webhook"`` are not supported yet — declare a custom
      AutomationRule for those.

    The generated payload carries ``{{stint.*}}`` placeholders; refs resolve
    at apply time like a hand-written rule's. "Allow rule trigger" stays
    off so the revert cannot re-trigger the rule.
    """

    alias: str
    name: str = ""
    description: str = ""
    fields: list[CustomField | str] | None = None
    allowed_writers: list[User | Group | Any] = field(default_factory=list)
    on_violation: list[str] = field(default_factory=lambda: ["revert", "comment"])
    enabled: bool = True
    # Declaring module for scoped autogenerate; recorded by the registry.
    _declared_module: str = field(default="", repr=False, compare=False)

    is_guard: ClassVar[bool] = True

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("ControlFieldGuard declaration requires a non-empty alias")
        if not self.name:
            self.name = self.alias
        if not self.allowed_writers:
            raise RegistryError(
                f"ControlFieldGuard {self.alias!r}: allowed_writers is required — a guard "
                f"with no allowed writers reverts every edit, including the service "
                f"account's. List at least the service account."
            )
        unknown = [v for v in self.on_violation if v not in ("revert", "comment")]
        if unknown:
            raise RegistryError(
                f"ControlFieldGuard {self.alias!r}: unsupported on_violation entries {unknown}; "
                f"supported: ['revert', 'comment'] (\"notify\"/\"webhook\" are not wired yet — "
                f"declare a custom AutomationRule for those)"
            )
        if "revert" not in self.on_violation:
            raise RegistryError(
                f"ControlFieldGuard {self.alias!r}: on_violation must include 'revert' — "
                f"a guard that only comments does not restore control fields"
            )
        for writer in self.allowed_writers:
            kind, _ = _ref_spec(writer)
            if kind == "system":
                raise RegistryError(
                    f"ControlFieldGuard {self.alias!r}: allowed_writers entries must be "
                    f"User, Group, or ProjectRole, got {writer!r}"
                )
        module = _declaring_module()
        self._declared_module = module
        registry.register_automation_rule(self)

    def field_refs(self) -> list[str]:
        """The declared field references guarded, explicit or derived.

        Explicit ``fields`` win. Derived fields need the bound project's
        screen declarations, so they resolve at autogenerate time via
        ``audit.declared_control_fields`` — an unbound guard with no
        explicit fields raises here with that explanation.
        """
        if self.fields is not None:
            return [_ref_spec(f)[1] for f in self.fields]
        raise RegistryError(
            f"ControlFieldGuard {self.alias!r}: no explicit fields and no bound project to "
            f"derive control fields from — bind the scheme to a Project or pass fields="
        )

    def writer_specs(self) -> list[tuple[str, str, str]]:
        """allowed_writers as (ref name, kind, declared value) triples.

        Ref names are positional (``writer0``...) and stable for a given
        declaration order, which is all the generated payload needs.
        """
        out: list[tuple[str, str, str]] = []
        for i, writer in enumerate(self.allowed_writers):
            kind, declared = _ref_spec(writer)
            out.append((f"writer{i}", kind, declared))
        return out

    def ref_specs(self) -> list[tuple[str, str, str]]:
        """Every ref the generated payload carries (writers only; fields go
        through the trigger/action forms, not refs)."""
        return self.writer_specs()

    def build_payload(self, fields: list[CustomField | str]) -> dict[str, Any]:
        """The rule payload with placeholders, for the given declared field refs."""
        return build_guard_payload(self, fields)


def guard_restore_value(field_ref: str, kind: str) -> Any:
    """The smart value that restores a field's previous value.

    Per Atlassian's docs ``{{fieldChange}}`` holds only the first changed
    value, so the generator uses the changelog form per field
    (``{{#changelog.<id>}}{{from}}{{/}}``): an edit that changes two guarded
    fields reverts both. The raw ``from`` is restored, not the
    ``fromString`` display text — select fields restore by option id,
    multi-value fields as the full previous set, user fields by accountId.
    The exact shapes are pinned in notes/automation_api_research.md with the
    per-type live checks still to run.
    """
    changelog = f"{{{{#changelog.{field_ref}}}}}{{{{from}}}}{{{{/}}}}"
    if kind == "user":
        return {"accountId": changelog}
    if kind == "group":
        return {"name": changelog}
    return changelog


def _restore_form_for_field(ref: str, field_obj: Any) -> tuple[str, Any]:
    """(field id form, restore value form) for one guarded field.

    ``ref``/``field_obj`` come from the guard's field list: a CustomField
    keeps its ``{{stint.field.<alias>}}`` placeholder (resolved at apply
    time); a system field name is inlined in Jira's lowercase id form.
    """
    if isinstance(field_obj, CustomField):
        placeholder = ref_placeholder("field", field_obj.alias)
        restore = _custom_field_restore(field_obj, placeholder)
        return placeholder, restore
    field_id = jira_system_field_id(ref)
    return field_id, guard_restore_value(field_id, "system")


def _custom_field_restore(cf: CustomField, placeholder: str) -> Any:
    """Restore form by declared field type (see ``guard_restore_value``)."""
    if cf.type is UserField:
        return guard_restore_value(placeholder, "user")
    if cf.type is GroupField:
        return guard_restore_value(placeholder, "group")
    if cf.type is LabelsField:
        return f'{{{{#changelog.{placeholder}}}}}{{{{from.split(" ")}}}}{{{{/}}}}'
    if cf.type in (MultiSelectField, CheckboxesField, MultiVersionField, MultiGroupField, MultiUserField):
        return f'{{{{#changelog.{placeholder}}}}}{{{{from.split(", ")}}}}{{{{/}}}}'
    return guard_restore_value(placeholder, "scalar")


def build_guard_payload(guard: ControlFieldGuard, fields: list[CustomField | str]) -> dict[str, Any]:
    """Generate the guard's rule payload (placeholders intact).

    Trigger: Field value changed on exactly the guarded field ids, filtered
    to edit operations. Condition: the initiator is not one of the allowed
    writers — the native user condition as the AND of negations (is-not the
    allowed users, not-in the allowed groups, not-in the allowed roles),
    which is De Morgan for "not (any allowed writer)". Actions: one Edit
    issue per guarded field restoring its previous value, then the optional
    comment. ``canOtherRuleTrigger`` stays off so the revert cannot
    re-trigger the rule — Automation's loop detection (10 rapid triggers)
    is not a substitute.
    """
    trigger_fields: list[dict[str, str]] = []
    restores: list[dict[str, str]] = []
    for field_obj in fields:
        ref = field_obj.alias if isinstance(field_obj, CustomField) else str(field_obj)
        field_form, restore = _restore_form_for_field(ref, field_obj)
        trigger_fields.append({"value": field_form, "type": "field"})
        restores.append({field_form: restore})

    conditions: list[dict[str, Any]] = []
    users = [w for w in guard.allowed_writers if isinstance(w, User)]
    groups = [w for w in guard.allowed_writers if isinstance(w, Group)]
    role_names = [w.name for w in _project_roles(guard.allowed_writers)]
    specs = guard.writer_specs()
    if users:
        criteria = [
            {"type": "ACCOUNT_ID", "value": ref_placeholder("user", name)} for name, kind, _ in specs if kind == "user"
        ]
        conditions.append({"field": "initiator", "check": CHECK_USER_IS_NOT, "criteria": criteria})
    if groups:
        criteria = [
            {"type": "NAME", "value": ref_placeholder("group", name)} for name, kind, _ in specs if kind == "group"
        ]
        conditions.append({"field": "initiator", "check": CHECK_NOT_IN_GROUP, "criteria": criteria})
    if role_names:
        criteria = [
            {"type": "NAME", "value": ref_placeholder("role", name)} for name, kind, _ in specs if kind == "role"
        ]
        conditions.append({"field": "initiator", "check": CHECK_NOT_IN_PROJECT_ROLE, "criteria": criteria})
    condition_component: dict[str, Any] | None = None
    if conditions:
        condition_component = {
            "component": "CONDITION",
            "parentId": None,
            "conditionParentId": None,
            "schemaVersion": 6,
            "type": CONDITION_USER,
            "value": {"conditions": conditions, "operator": "AND"},
            "children": [],
            "conditions": [],
            "connectionId": None,
        }

    actions: list[dict[str, Any]] = []
    if restores:
        advanced: dict[str, Any] = {"fields": {}}
        for restore in restores:
            advanced["fields"].update(restore)
        actions.append(
            {
                "component": "ACTION",
                "parentId": None,
                "conditionParentId": None,
                "schemaVersion": 12,
                "type": ACTION_EDIT_ISSUE,
                "value": {
                    "operations": [],
                    "advancedFields": json.dumps(advanced, indent=2),
                    "sendNotifications": False,
                },
                "children": [],
                "conditions": [],
                "connectionId": None,
            }
        )
    if "comment" in guard.on_violation:
        actions.append(
            {
                "component": "ACTION",
                "parentId": None,
                "conditionParentId": None,
                "schemaVersion": 2,
                "type": ACTION_COMMENT,
                "value": {
                    "comment": (
                        "Reverted an edit to the protected field(s) by "
                        "[~accountid:{{initiator.accountId}}]: the previous value was restored."
                    ),
                    "publicComment": False,
                    "commentVisibility": None,
                    "sendNotifications": False,
                    "addCommentOnce": False,
                },
                "children": [],
                "conditions": [],
                "connectionId": None,
            }
        )

    components = [condition_component] if condition_component is not None else []
    components.extend(actions)
    return {
        "name": guard.name,
        "description": guard.description,
        "trigger": {
            "component": "TRIGGER",
            "parentId": None,
            "conditionParentId": None,
            "schemaVersion": 2,
            "type": TRIGGER_FIELD_CHANGED,
            "value": {
                "changeType": "ANY_CHANGE",
                "fields": trigger_fields,
                "actions": list(_GUARD_TRIGGER_OPERATIONS),
            },
            "children": [],
            "conditions": [],
            "connectionId": None,
        },
        "components": components,
        "canOtherRuleTrigger": False,
        "notifyOnError": "FIRSTERROR",
        "labels": [],
        "writeAccessType": "UNRESTRICTED",
    }


@dataclass
class AutomationRuleScheme:
    """A set of automation rules managed together and scoped to the bound
    projects.

    The binding is ``Project.__automation_rule_scheme__`` (authoritative);
    ``project`` is an optional project key hint like other scheme types.
    A scheme bound to several projects produces one multi-project rule
    scope, not copies.
    """

    alias: str
    name: str = ""
    description: str = ""
    rules: list[AutomationRule | ControlFieldGuard] = field(default_factory=list)
    project: str | None = None  # project key hint (non-authoritative)

    def __post_init__(self) -> None:
        if not self.alias:
            raise RegistryError("AutomationRuleScheme declaration requires a non-empty alias")
        if not self.name:
            self.name = self.alias
        names = [rule.name for rule in self.rules]
        if len(set(names)) != len(names):
            raise RegistryError(f"AutomationRuleScheme {self.alias!r}: duplicate rule names {sorted(names)}")
        registry.register_automation_rule_scheme(self)
