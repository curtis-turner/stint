"""Jira Cloud dialect for company-managed projects.

Team-managed writes live in the separate ``stint.dialects.jira.tmp`` dialect.
"""

from __future__ import annotations

import dataclasses
import warnings
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from stint.client.http import JiraHTTPClient
from stint.dialects.jira import common
from stint.exceptions import (
    ConfigurationError,
    PermissionError,
    ReflectionError,
    TransportError,
)
from stint.state.snapshot import (
    CustomFieldSnapshot,
    FieldConfigurationItemSnapshot,
    FieldConfigurationSchemeSnapshot,
    FieldConfigurationSnapshot,
    IssueTypeSchemeSnapshot,
    IssueTypeScreenSchemeSnapshot,
    IssueTypeSnapshot,
    ProjectSnapshot,
    ScreenSchemeSnapshot,
    ScreenSnapshot,
    ScreenTabSnapshot,
    SecurityLevelMemberSnapshot,
    SecurityLevelSnapshot,
    SecuritySchemeSnapshot,
    ServerInfoSnapshot,
    Snapshot,
)


class JiraCloudDialect:
    """Jira Cloud dialect. Company-managed and team-managed projects alike
    share this surface; team-managed writes live in a separate dialect
    (see ``stint.dialects.jira.tmp``).
    """

    name: ClassVar[str] = "jira_cloud"
    api_root: ClassVar[str] = "/rest/api/3"
    expected_deployment_type: ClassVar[str] = "Cloud"
    field_config_items_segment: ClassVar[str] = "fields"
    user_search_param: ClassVar[str] = "query"
    user_search_id_field: ClassVar[str] = "accountId"

    # Field configuration API generations. Atlassian retired classic field
    # configurations on Jira Cloud in May 2026 and replaced them with the
    # Field Schemes API. The rollout was progressive, so two Cloud tenants on
    # the same stint version can legitimately need different calls; the
    # dialect probes once per engine instance and routes accordingly.
    FIELD_CONFIG_API_V1: ClassVar[str] = "v1"
    FIELD_CONFIG_API_V2: ClassVar[str] = "v2"
    FIELD_SCHEMES_SEGMENT: ClassVar[str] = "config/fieldschemes"
    # The v2 bulk endpoints cap each request at 100 top-level field ids.
    FIELD_SCHEME_BATCH_SIZE: ClassVar[int] = 100

    def __init__(self, client: JiraHTTPClient) -> None:
        self.client = client
        self._lead_cache: dict[str, str] = {}
        self._field_config_api_cache: str | None = None
        self._all_field_ids_cache: frozenset[str] | None = None
        self._work_type_ids_cache: frozenset[str] | None = None

    # ── Detection ────────────────────────────────────────────────────
    async def detect(self) -> bool:
        info = await self._server_info()
        if info.deployment_type != self.expected_deployment_type:
            return False
        # Warm the per-tenant field-configuration API choice now that we know
        # this is a Cloud tenant. Cached for the life of the dialect instance.
        await self.field_config_api()
        return True

    async def _server_info(self) -> ServerInfoSnapshot:
        payload = await self.client.get_json(f"{self.api_root}/serverInfo")
        return ServerInfoSnapshot(
            deployment_type=str(payload.get("deploymentType", "")),
            version=str(payload.get("version", "")),
            base_url=str(payload.get("baseUrl", self.client.base_url)),
        )

    # ── Field configuration API capability detection ─────────────────
    async def field_config_api(self, *, force: bool = False) -> str:
        """Return which field-configuration API this tenant speaks.

        ``v2`` means Field Schemes (``/config/fieldschemes``), ``v1`` means
        the classic field-configuration endpoints. Probed once and cached on
        the dialect instance; pass ``force=True`` to re-probe after a
        retired-API error.

        A failed probe intentionally falls back to ``v1`` rather than
        raising. Tenants that predate the Field Schemes feature flag answer
        404/400 here, and a transient failure should not make every
        field-configuration read fail. If v1 has actually been retired, the
        create path detects the specific 400 and retries on v2 (see
        ``_probe_after_retired_error``).
        """
        if self._field_config_api_cache is not None and not force:
            return self._field_config_api_cache
        try:
            await self.client.get_json(
                f"{self.api_root}/{self.FIELD_SCHEMES_SEGMENT}",
                params={"startAt": 0, "maxResults": 1},
            )
        except Exception:  # noqa: BLE001 - capability probes must never break a run
            version = self.FIELD_CONFIG_API_V1
        else:
            version = self.FIELD_CONFIG_API_V2
        self._field_config_api_cache = version
        return version

    async def _field_config_api_is_v2(self) -> bool:
        return await self.field_config_api() == self.FIELD_CONFIG_API_V2

    def _use_field_schemes(self) -> None:
        self._field_config_api_cache = self.FIELD_CONFIG_API_V2

    async def _probe_after_retired_error(self) -> bool:
        """Re-probe after the classic create endpoint rejects a write.

        Returns True when the tenant actually speaks Field Schemes, so the
        caller can retry there instead of surfacing the retirement error.
        """
        return await self.field_config_api(force=True) == self.FIELD_CONFIG_API_V2

    # ── Full reflection ──────────────────────────────────────────────
    async def reflect(self) -> Snapshot:
        info = await self._server_info()
        custom_fields = await self._reflect_custom_fields()
        issuetypes = await self._reflect_issuetypes()
        projects = await self._reflect_projects()
        screens = await self._reflect_screens()
        screen_schemes = await self._reflect_screen_schemes()
        its = await self._reflect_issuetype_schemes()
        itss = await self._reflect_issuetype_screen_schemes()
        field_configurations = await self._reflect_field_configurations()
        fcs = await self._reflect_field_configuration_schemes()
        security_schemes = await self._reflect_issue_security_schemes()
        return Snapshot(
            server_info=info,
            custom_fields=custom_fields,
            issuetypes=issuetypes,
            projects=projects,
            screens=screens,
            screen_schemes=screen_schemes,
            issuetype_schemes=its,
            issuetype_screen_schemes=itss,
            field_configurations=field_configurations,
            field_configuration_schemes=fcs,
            security_schemes=security_schemes,
        )

    # ── Custom fields ────────────────────────────────────────────────
    async def _iter_custom_field_payloads(self) -> AsyncIterator[dict[str, Any]]:
        """Cloud: page ``GET /field/search`` (``type=custom``).

        The non-paginated ``GET /field`` returns only a subset of custom
        fields on Cloud (it omits fields not yet associated with a screen,
        including freshly created ones), so reflect missed fields stint had
        just created. The paginated field-search endpoint returns the full
        custom-field set and includes the ``schema`` needed for type
        detection. (#9)
        """
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/field/search",
            extra_params={"type": "custom"},
        ):
            yield entry

    async def _reflect_custom_fields(self) -> dict[str, CustomFieldSnapshot]:
        result: dict[str, CustomFieldSnapshot] = {}
        async for entry in self._iter_custom_field_payloads():
            if not common.is_custom_field(entry):
                continue
            cf = common.parse_custom_field(entry)
            if common.is_select_style(cf.type_id):
                options = await self._reflect_field_options(cf.id)
                cf = CustomFieldSnapshot(
                    id=cf.id,
                    name=cf.name,
                    type_id=cf.type_id,
                    options=options,
                )
            result[cf.id] = cf
        return result

    async def _reflect_field_options(self, field_id: str) -> dict[str, str]:
        """Cloud: option fetch goes through the field context.

        Order: list contexts for the field, pick the first one (the default
        context), then list options under that context. Multi-context fields
        are reflected against the default only in 0.1; full multi-context
        support is a known gap.
        """
        default_ctx_id = await self._default_context_id(field_id)
        if not default_ctx_id:
            return {}
        opts: list[dict[str, Any]] = []
        async for opt in common.paginate(
            self.client,
            f"{self.api_root}/field/{field_id}/context/{default_ctx_id}/option",
        ):
            opts.append(opt)
        return common.parse_field_options(opts)

    # ── Issue types ──────────────────────────────────────────────────
    async def _reflect_issuetypes(self) -> dict[str, IssueTypeSnapshot]:
        raw = await self.client.get_json(f"{self.api_root}/issuetype")
        if not isinstance(raw, list):
            raise ReflectionError(f"{self.api_root}/issuetype returned non-list: {type(raw)}")
        return {it.id: it for it in (common.parse_issuetype(p) for p in raw)}

    # ── Projects ─────────────────────────────────────────────────────
    async def _reflect_projects(self) -> dict[str, ProjectSnapshot]:
        result: dict[str, ProjectSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/project/search"):
            proj = common.parse_project(entry)
            result[proj.key] = proj
        if not result:
            return result

        # Resolve per-project scheme bindings so the diff can detect drift
        # against existing projects (otherwise UpdateProject fires but no
        # SetProject*Scheme — schema-declared schemes go unbound).
        project_ids = [p.id for p in result.values()]
        its_map = await self._reflect_project_scheme_bindings(
            f"{self.api_root}/issuetypescheme/project",
            project_ids=project_ids,
            scheme_key="issueTypeScheme",
        )
        itss_map = await self._reflect_project_scheme_bindings(
            f"{self.api_root}/issuetypescreenscheme/project",
            project_ids=project_ids,
            scheme_key="issueTypeScreenScheme",
        )
        fcs_map = await self._reflect_project_field_configuration_bindings(project_ids)
        for key, proj in list(result.items()):
            result[key] = dataclasses.replace(
                proj,
                issuetype_scheme_id=its_map.get(proj.id),
                issuetype_screen_scheme_id=itss_map.get(proj.id),
                field_configuration_scheme_id=fcs_map.get(proj.id),
            )
        return result

    async def _reflect_project_field_configuration_bindings(
        self,
        project_ids: list[str],
    ) -> dict[str, str]:
        """project_id → field-scheme/configuration id map, for whichever API."""
        if not project_ids:
            return {}
        if not await self._field_config_api_is_v2():
            return await self._reflect_project_scheme_bindings(
                f"{self.api_root}/fieldconfigurationscheme/project",
                project_ids=project_ids,
                scheme_key="fieldConfigurationScheme",
            )
        out: dict[str, str] = {}
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/{self.FIELD_SCHEMES_SEGMENT}/projects",
            extra_params={"projectId": [_as_api_int(pid) for pid in project_ids]},
        ):
            project_id = entry.get("projectId")
            scheme_id = entry.get("schemeId")
            if project_id is not None and scheme_id is not None:
                out[str(project_id)] = str(scheme_id)
        return out

    async def _reflect_project_scheme_bindings(
        self,
        path: str,
        *,
        project_ids: list[str],
        scheme_key: str,
    ) -> dict[str, str]:
        """Inverts Atlassian's project-scheme listing into a project_id→scheme_id map.

        The endpoint returns ``{values: [{<scheme_key>: {id, ...}, projectIds: [...]}, ...]}``.
        Entries without ``<scheme_key>`` are treated as "no explicit binding" and skipped
        — those projects use Jira's default scheme and the ProjectSnapshot field stays None.
        """
        out: dict[str, str] = {}
        async for entry in common.paginate(
            self.client,
            path,
            extra_params={"projectId": project_ids},
        ):
            scheme = entry.get(scheme_key)
            if not isinstance(scheme, dict):
                continue
            scheme_id = scheme.get("id")
            if scheme_id is None:
                continue
            for pid in entry.get("projectIds") or []:
                out[str(pid)] = str(scheme_id)
        return out

    # ── Screens ──────────────────────────────────────────────────────
    async def _reflect_screens(self) -> dict[str, ScreenSnapshot]:
        screens: dict[str, ScreenSnapshot] = {}
        skipped: list[str] = []
        async for entry in common.paginate(self.client, f"{self.api_root}/screens"):
            header = common.parse_screen_header(entry)
            try:
                tabs = await self._reflect_screen_tabs(header.id)
            except TransportError as e:
                # Cloud tenants with team-managed projects expose synthetic
                # screens in /screens whose /tabs endpoint returns 400
                # "Screen with id N does not exist". TMP does not use the
                # screens system, and these are never stint-managed. Collect
                # them and emit one summary instead of a per-screen warning
                # that buries the real output (a busy tenant has dozens).
                if e.status_code == 400:
                    skipped.append(f"{header.id} ({header.name!r})")
                    continue
                raise
            screens[header.id] = ScreenSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                tabs=tuple(tabs),
            )
        if skipped:
            warnings.warn(
                f"reflection skipped {len(skipped)} team-managed synthetic "
                f"screen(s) (their /tabs endpoint returns 400; not "
                f"stint-managed): {', '.join(skipped)}",
                stacklevel=2,
            )
        return screens

    async def _reflect_screen_tabs(self, screen_id: str) -> list[ScreenTabSnapshot]:
        raw_tabs = await self.client.get_json(f"{self.api_root}/screens/{screen_id}/tabs")
        if not isinstance(raw_tabs, list):
            raise ReflectionError(f"{self.api_root}/screens/{screen_id}/tabs returned non-list")
        out: list[ScreenTabSnapshot] = []
        for tab in raw_tabs:
            tab_id = str(tab.get("id", ""))
            raw_fields = await self.client.get_json(f"{self.api_root}/screens/{screen_id}/tabs/{tab_id}/fields")
            if not isinstance(raw_fields, list):
                raise ReflectionError(f"tab {tab_id} on screen {screen_id} returned non-list fields")
            out.append(common.parse_screen_tab(tab, raw_fields))
        return out

    # ── Screen schemes ───────────────────────────────────────────────
    async def _reflect_screen_schemes(self) -> dict[str, ScreenSchemeSnapshot]:
        result: dict[str, ScreenSchemeSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/screenscheme"):
            ss = common.parse_screen_scheme(entry)
            result[ss.id] = ss
        return result

    # ── Issue-type screen schemes ────────────────────────────────────
    async def _reflect_issuetype_screen_schemes(
        self,
    ) -> dict[str, IssueTypeScreenSchemeSnapshot]:
        result: dict[str, IssueTypeScreenSchemeSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/issuetypescreenscheme"):
            header = common.parse_itss_header(entry)
            mappings = await self._reflect_itss_mappings(header.id)
            result[header.id] = IssueTypeScreenSchemeSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                mappings=tuple(mappings),
            )
        return result

    async def _reflect_itss_mappings(self, scheme_id: str):
        out = []
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/issuetypescreenscheme/mapping",
            extra_params={"issueTypeScreenSchemeId": scheme_id},
        ):
            out.append(common.parse_itss_mapping(entry))
        return out

    # ── Issue type schemes ───────────────────────────────────────────
    async def _reflect_issuetype_schemes(self) -> dict[str, IssueTypeSchemeSnapshot]:
        """Reflect global IssueTypeSchemes and their member issuetypes.

        Cloud serves the scheme list at /issuetypescheme and the per-scheme
        member list at /issuetypescheme/mapping (paginated, filterable by
        scheme id).
        """
        result: dict[str, IssueTypeSchemeSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/issuetypescheme"):
            header = common.parse_issuetype_scheme_header(entry)
            member_ids = await self._reflect_issuetype_scheme_members(header.id)
            result[header.id] = IssueTypeSchemeSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                issuetype_ids=tuple(member_ids),
                default_issuetype_id=header.default_issuetype_id,
            )
        return result

    async def _reflect_issuetype_scheme_members(self, scheme_id: str) -> list[str]:
        """Cloud: list issuetype IDs belonging to a scheme via /issuetypescheme/mapping."""
        out: list[str] = []
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/issuetypescheme/mapping",
            extra_params={"issueTypeSchemeId": scheme_id},
        ):
            row_scheme_id, row_issuetype_id = common.parse_issuetype_scheme_mapping(entry)
            if row_scheme_id == scheme_id and row_issuetype_id:
                out.append(row_issuetype_id)
        return out

    # ── Field configurations ─────────────────────────────────────────
    async def _reflect_field_configurations(self) -> dict[str, FieldConfigurationSnapshot]:
        if await self._field_config_api_is_v2():
            return await self._reflect_field_schemes_as_configurations()
        result: dict[str, FieldConfigurationSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/fieldconfiguration"):
            header = common.parse_field_configuration_header(entry)
            items = await self._reflect_field_configuration_items(header.id)
            result[header.id] = FieldConfigurationSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                items=items,
            )
        return result

    async def _reflect_field_schemes_as_configurations(
        self,
    ) -> dict[str, FieldConfigurationSnapshot]:
        """Reflect v2 field schemes into the classic field-configuration shape.

        The higher layers (diff, autogen, state) still speak in terms of
        ``FieldConfiguration`` aliases. A v2 field scheme is the direct
        replacement for one classic field configuration, so the shape maps
        one-to-one; fields absent from the scheme simply do not appear in
        ``items`` (the v2 equivalent of hidden/default-config entries).
        """
        result: dict[str, FieldConfigurationSnapshot] = {}
        async for entry in self._iter_field_scheme_payloads():
            header = common.parse_field_configuration_header(entry)
            items = await self._reflect_field_configuration_items(header.id)
            result[header.id] = FieldConfigurationSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                items=items,
            )
        return result

    async def _reflect_field_configuration_items(self, fc_id: str) -> dict[str, FieldConfigurationItemSnapshot]:
        if await self._field_config_api_is_v2():
            return await self._reflect_field_scheme_items(fc_id)
        items: dict[str, FieldConfigurationItemSnapshot] = {}
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/fieldconfiguration/{fc_id}/{self.field_config_items_segment}",
        ):
            item = common.parse_field_configuration_item(entry)
            if item.field_id:
                items[item.field_id] = item
        return items

    async def _reflect_field_scheme_items(self, scheme_id: str) -> dict[str, FieldConfigurationItemSnapshot]:
        items: dict[str, FieldConfigurationItemSnapshot] = {}
        async for entry in self.get_field_scheme_fields(scheme_id):
            item = common.parse_field_scheme_field(entry)
            if item.field_id:
                items[item.field_id] = item
        return items

    # ── Field configuration schemes ──────────────────────────────────
    async def _reflect_field_configuration_schemes(
        self,
    ) -> dict[str, FieldConfigurationSchemeSnapshot]:
        if await self._field_config_api_is_v2():
            return await self._reflect_field_schemes_as_schemes()
        result: dict[str, FieldConfigurationSchemeSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/fieldconfigurationscheme"):
            header = common.parse_fcs_header(entry)
            mappings = await self._reflect_fcs_mappings(header.id)
            result[header.id] = FieldConfigurationSchemeSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                mappings=tuple(mappings),
            )
        return result

    async def _reflect_field_schemes_as_schemes(self) -> dict[str, FieldConfigurationSchemeSnapshot]:
        """v2 has no scheme-of-configurations layer.

        Each field scheme is its own project/work-type binding, so it is also
        the thing the classic ``FieldConfigurationScheme`` slot refers to.
        Mappings stay empty: v2 expresses per-work-type behavior as inline
        field parameters rather than a work-type → configuration table, and
        the diff knows not to compare an empty mapping table (see
        ``_fcs_mappings_changed``).
        """
        result: dict[str, FieldConfigurationSchemeSnapshot] = {}
        async for entry in self._iter_field_scheme_payloads():
            header = common.parse_field_configuration_header(entry)
            result[header.id] = FieldConfigurationSchemeSnapshot(
                id=header.id,
                name=header.name,
                description=header.description,
                mappings=(),
            )
        return result

    async def _reflect_fcs_mappings(self, scheme_id: str):
        out = []
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/fieldconfigurationscheme/mapping",
            extra_params={"fieldConfigurationSchemeId": scheme_id},
        ):
            out.append(common.parse_fcs_mapping(entry))
        return out

    # ── Write-side operations (used by op API) ───────────────────────
    async def create_custom_field(
        self,
        *,
        name: str,
        description: str,
        type_id: str,
        searcher_key: str | None = None,
    ) -> str:
        """POST /field. Returns the new field id (e.g. ``customfield_10042``)."""
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "type": type_id,
        }
        if searcher_key:
            body["searcherKey"] = searcher_key
        result = await self.client.post_json(f"{self.api_root}/field", json=body)
        if not isinstance(result, dict) or "id" not in result:
            raise ReflectionError(f"POST /field returned no id: {result!r}")
        self._all_field_ids_cache = None
        return str(result["id"])

    async def add_custom_field_option(self, field_id: str, value: str) -> str:
        """Cloud: POST options through the field context."""
        ctx_id = await self._default_context_id(field_id)
        if not ctx_id:
            raise ReflectionError(f"Cloud field {field_id} has no context; cannot add option {value!r}")
        # The Cloud option endpoint accepts an array per call.
        result = await self.client.post_json(
            f"{self.api_root}/field/{field_id}/context/{ctx_id}/option",
            json={"options": [{"value": value}]},
        )
        values = (result or {}).get("options") or []
        if not values or "id" not in values[0]:
            raise ReflectionError(f"Cloud add-option for {field_id} returned no id: {result!r}")
        return str(values[0]["id"])

    async def delete_custom_field(self, field_id: str) -> None:
        await self.client.delete(f"{self.api_root}/field/{field_id}")
        self._all_field_ids_cache = None

    async def update_custom_field(
        self,
        field_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        searcher_key: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if searcher_key is not None:
            body["searcherKey"] = searcher_key
        if not body:
            return
        await self.client.put_json(f"{self.api_root}/field/{field_id}", json=body)

    async def delete_custom_field_option(
        self,
        field_id: str,
        option_id: str,
    ) -> None:
        """Cloud: DELETE option through the field context."""
        ctx_id = await self._default_context_id(field_id)
        if not ctx_id:
            raise ReflectionError(f"Cloud field {field_id} has no context; cannot delete option {option_id!r}")
        await self.client.delete(f"{self.api_root}/field/{field_id}/context/{ctx_id}/option/{option_id}")

    async def _default_context_id(self, field_id: str) -> str | None:
        contexts: list[dict[str, Any]] = []
        async for ctx in common.paginate(self.client, f"{self.api_root}/field/{field_id}/context"):
            contexts.append(ctx)
        if not contexts:
            return None
        return str(contexts[0].get("id", "")) or None

    # ── Screens ──────────────────────────────────────────────────────
    async def create_screen(self, *, name: str, description: str = "") -> str:
        body: dict[str, Any] = {"name": name, "description": description}
        result = await self.client.post_json(f"{self.api_root}/screens", json=body)
        return _expect_id(result, f"POST {self.api_root}/screens")

    async def delete_screen(self, screen_id: str) -> None:
        await self.client.delete(f"{self.api_root}/screens/{screen_id}")

    async def update_screen(
        self,
        screen_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(f"{self.api_root}/screens/{screen_id}", json=body)

    async def add_screen_tab(self, screen_id: str, *, name: str) -> str:
        result = await self.client.post_json(
            f"{self.api_root}/screens/{screen_id}/tabs",
            json={"name": name},
        )
        return _expect_id(result, f"POST /screens/{screen_id}/tabs")

    async def add_screen_tab_field(
        self,
        screen_id: str,
        tab_id: str,
        *,
        field_id: str,
    ) -> None:
        await self.client.post_json(
            f"{self.api_root}/screens/{screen_id}/tabs/{tab_id}/fields",
            json={"fieldId": field_id},
        )

    # ── Screen schemes ───────────────────────────────────────────────
    async def create_screen_scheme(
        self,
        *,
        name: str,
        description: str,
        screens: dict[str, str],
    ) -> str:
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "screens": screens,
        }
        result = await self.client.post_json(f"{self.api_root}/screenscheme", json=body)
        return _expect_id(result, f"POST {self.api_root}/screenscheme")

    async def delete_screen_scheme(self, scheme_id: str) -> None:
        await self.client.delete(f"{self.api_root}/screenscheme/{scheme_id}")

    async def update_screen_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        screens: dict[str, str] | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if screens is not None:
            body["screens"] = screens
        if not body:
            return
        await self.client.put_json(
            f"{self.api_root}/screenscheme/{scheme_id}",
            json=body,
        )

    # ── Issue-type screen schemes ────────────────────────────────────
    async def create_issuetype_screen_scheme(
        self,
        *,
        name: str,
        description: str,
        mappings: list[dict[str, str]],
    ) -> str:
        """mappings: [{"issueTypeId": "...", "screenSchemeId": "..."}, ...].

        Must include exactly one entry with issueTypeId == "default".
        """
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "issueTypeMappings": mappings,
        }
        result = await self.client.post_json(
            f"{self.api_root}/issuetypescreenscheme",
            json=body,
        )
        return _expect_id(result, f"POST {self.api_root}/issuetypescreenscheme")

    async def delete_issuetype_screen_scheme(self, scheme_id: str) -> None:
        await self.client.delete(f"{self.api_root}/issuetypescreenscheme/{scheme_id}")

    async def update_issuetype_screen_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(
            f"{self.api_root}/issuetypescreenscheme/{scheme_id}",
            json=body,
        )

    async def set_issuetype_screen_scheme_mappings(
        self,
        scheme_id: str,
        *,
        mappings: list[dict[str, str]],
    ) -> None:
        """Replace ITSS mappings. mappings: [{"issueTypeId": "...", "screenSchemeId": "..."}, ...]."""
        await self.client.put_json(
            f"{self.api_root}/issuetypescreenscheme/{scheme_id}/mapping",
            json={"issueTypeMappings": mappings},
        )

    async def set_project_issuetype_screen_scheme(
        self,
        *,
        project_id: str,
        scheme_id: str,
    ) -> None:
        await self.client.put_json(
            f"{self.api_root}/issuetypescreenscheme/project",
            json={"issueTypeScreenSchemeId": scheme_id, "projectId": project_id},
        )

    # ── Issue type schemes ───────────────────────────────────────────
    async def create_issuetype_scheme(
        self,
        *,
        name: str,
        issuetype_ids: list[str],
        default_issuetype_id: str,
        description: str = "",
    ) -> str:
        """Create an IssueTypeScheme. ``issuetype_ids`` must contain at least
        one standard (non-subtask) issuetype id, and ``default_issuetype_id``
        must be in that list. Jira enforces both constraints."""
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "issueTypeIds": list(issuetype_ids),
            "defaultIssueTypeId": default_issuetype_id,
        }
        result = await self.client.post_json(
            f"{self.api_root}/issuetypescheme",
            json=body,
        )
        # Cloud returns {"issueTypeSchemeId": "..."} from this endpoint, unlike
        # most create endpoints which return {"id": ...}.
        if isinstance(result, dict) and "issueTypeSchemeId" in result:
            return str(result["issueTypeSchemeId"])
        return _expect_id(result, f"POST {self.api_root}/issuetypescheme")

    async def update_issuetype_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        default_issuetype_id: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if default_issuetype_id is not None:
            body["defaultIssueTypeId"] = default_issuetype_id
        if not body:
            return
        await self.client.put_json(
            f"{self.api_root}/issuetypescheme/{scheme_id}",
            json=body,
        )

    async def delete_issuetype_scheme(self, scheme_id: str) -> None:
        await self.client.delete(f"{self.api_root}/issuetypescheme/{scheme_id}")

    async def set_project_issuetype_scheme(
        self,
        *,
        project_id: str,
        scheme_id: str,
    ) -> None:
        await self.client.put_json(
            f"{self.api_root}/issuetypescheme/project",
            json={"issueTypeSchemeId": scheme_id, "projectId": project_id},
        )

    # ── Field configurations (classic v1) / field schemes (v2) ───────
    async def create_field_configuration(
        self,
        *,
        name: str,
        description: str = "",
    ) -> str:
        if await self._field_config_api_is_v2():
            scheme_id = await self.create_field_scheme(name=name, description=description)
            # Classic field configurations show every field unless it is
            # explicitly hidden. A new v2 scheme only seeds a minimal set of
            # critical fields, so associate the rest up front; hidden items
            # remove their associations in ``set_field_configuration_item``.
            await self._associate_all_fields_best_effort(scheme_id)
            return scheme_id
        body: dict[str, Any] = {"name": name, "description": description}
        try:
            result = await self.client.post_json(
                f"{self.api_root}/fieldconfiguration",
                json=body,
            )
        except TransportError as exc:
            # The rollout is progressive: a tenant may have been migrated
            # between our probe and this write. Re-probe and retry on v2
            # before falling back to the actionable retirement error.
            if _is_retired_field_configuration_error(exc) and await self._probe_after_retired_error():
                self._use_field_schemes()
                return await self.create_field_scheme(name=name, description=description)
            raise _maybe_wrap_retired_field_configuration(exc) from exc
        return _expect_id(result, f"POST {self.api_root}/fieldconfiguration")

    async def delete_field_configuration(self, fc_id: str) -> None:
        if await self._field_config_api_is_v2():
            await self.delete_field_scheme(fc_id)
            return
        await self.client.delete(f"{self.api_root}/fieldconfiguration/{fc_id}")

    async def update_field_configuration(
        self,
        fc_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Rename/re-describe a classic field configuration or a field scheme."""
        if await self._field_config_api_is_v2():
            await self.update_field_scheme(fc_id, name=name, description=description)
            return
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(f"{self.api_root}/fieldconfiguration/{fc_id}", json=body)

    async def set_field_configuration_item(
        self,
        fc_id: str,
        *,
        field_id: str,
        required: bool = False,
        hidden: bool = False,
        description: str = "",
    ) -> None:
        if await self._field_config_api_is_v2():
            await self._set_field_scheme_item(
                fc_id,
                field_id=field_id,
                required=required,
                hidden=hidden,
                description=description,
            )
            return
        await self.client.put_json(
            f"{self.api_root}/fieldconfiguration/{fc_id}/fields",
            json={
                "fieldConfigurationItems": [
                    {
                        "id": field_id,
                        "isRequired": required,
                        "isHidden": hidden,
                        "description": description,
                    }
                ]
            },
        )

    async def _set_field_scheme_item(
        self,
        scheme_id: str,
        *,
        field_id: str,
        required: bool,
        hidden: bool,
        description: str,
    ) -> None:
        """Translate one classic configuration item onto a v2 field scheme.

        v2 has no ``isHidden`` flag: hiding a field means not associating it
        with the scheme (its absence is the equivalent of hidden). A field
        marked both hidden and required is treated as hidden; Jira's classic
        model rejected that combination too.
        """
        if hidden:
            await self.remove_fields_from_field_scheme(scheme_id, field_ids=[field_id])
            return
        await self.associate_fields_with_field_scheme(scheme_id, field_ids=[field_id])
        await self.set_field_scheme_field_parameters(
            scheme_id,
            field_id=field_id,
            required=required,
            description=description,
        )

    # ── Field configuration schemes (v1) / field schemes (v2) ────────
    async def create_field_configuration_scheme(
        self,
        *,
        name: str,
        description: str = "",
    ) -> str:
        if await self._field_config_api_is_v2():
            return await self.create_field_scheme(name=name, description=description)
        body: dict[str, Any] = {"name": name, "description": description}
        result = await self.client.post_json(
            f"{self.api_root}/fieldconfigurationscheme",
            json=body,
        )
        return _expect_id(result, f"POST {self.api_root}/fieldconfigurationscheme")

    async def delete_field_configuration_scheme(self, scheme_id: str) -> None:
        if await self._field_config_api_is_v2():
            await self.delete_field_scheme(scheme_id)
            return
        await self.client.delete(f"{self.api_root}/fieldconfigurationscheme/{scheme_id}")

    async def update_field_configuration_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        if await self._field_config_api_is_v2():
            await self.update_field_scheme(scheme_id, name=name, description=description)
            return
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(
            f"{self.api_root}/fieldconfigurationscheme/{scheme_id}",
            json=body,
        )

    async def set_field_configuration_scheme_mappings(
        self,
        scheme_id: str,
        *,
        mappings: list[dict[str, str]],
    ) -> None:
        """mappings: [{"issueTypeId": "...", "fieldConfigurationId": "..."}, ...].

        At least one entry must have issueTypeId == "default".
        """
        if await self._field_config_api_is_v2():
            await self._set_field_scheme_mappings(scheme_id, mappings=mappings)
            return
        await self.client.put_json(
            f"{self.api_root}/fieldconfigurationscheme/{scheme_id}/mapping",
            json={"mappings": mappings},
        )

    async def set_project_field_configuration_scheme(
        self,
        *,
        project_id: str,
        scheme_id: str,
    ) -> None:
        if await self._field_config_api_is_v2():
            await self.associate_projects_with_field_scheme(
                scheme_id,
                project_ids=[project_id],
            )
            return
        await self.client.put_json(
            f"{self.api_root}/fieldconfigurationscheme/project",
            json={"fieldConfigurationSchemeId": scheme_id, "projectId": project_id},
        )

    # ── Field Schemes API (Cloud v2, /config/fieldschemes) ───────────
    def _field_schemes_path(self, *parts: str) -> str:
        suffix = "/".join(str(p) for p in parts if p != "")
        base = f"{self.api_root}/{self.FIELD_SCHEMES_SEGMENT}"
        return f"{base}/{suffix}" if suffix else base

    async def _iter_field_scheme_payloads(
        self,
        *,
        project_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        extra_params = {"projectId": [_as_api_int(pid) for pid in project_ids]} if project_ids else None
        async for entry in common.paginate(
            self.client,
            self._field_schemes_path(),
            extra_params=extra_params,
        ):
            yield entry

    async def _all_field_ids(self) -> frozenset[str]:
        """Every system/custom field id, cached for the dialect session.

        ``GET /field`` omits custom fields that are not on a screen yet, so
        union it with the paginated custom-field search stint already uses
        for reflection. A failed lookup returns an empty set: callers use
        this only to *broaden* associations, never to validate a write.
        """
        if self._all_field_ids_cache is not None:
            return self._all_field_ids_cache
        try:
            raw = await self.client.get_json(f"{self.api_root}/field")
        except Exception:  # noqa: BLE001 - broadening association is best-effort
            return frozenset()
        ids = (
            {str(entry["id"]) for entry in raw if isinstance(entry, dict) and entry.get("id")}
            if isinstance(raw, list)
            else set()
        )
        try:
            async for entry in self._iter_custom_field_payloads():
                if entry.get("id"):
                    ids.add(str(entry["id"]))
        except Exception:  # noqa: BLE001 - /field already gave the system fields
            pass
        self._all_field_ids_cache = frozenset(ids)
        return self._all_field_ids_cache

    async def _associate_all_fields_best_effort(self, scheme_id: str) -> None:
        """Associate every field with a newly created field scheme.

        This is the v2 equivalent of a classic field configuration's
        "everything visible unless hidden" default. Association failures are
        reported as a warning rather than raised: the scheme exists and the
        required/hidden items that follow are the part the schema author
        actually declared, so a partial association should not abort apply.
        """
        field_ids = sorted(await self._all_field_ids())
        if not field_ids:
            return
        try:
            await self.associate_fields_with_field_scheme(scheme_id, field_ids=field_ids)
        except Exception as exc:  # noqa: BLE001 - see docstring
            warnings.warn(
                f"could not associate all fields with field scheme {scheme_id}: {exc}",
                stacklevel=2,
            )

    async def _all_work_type_ids(self) -> frozenset[str]:
        """Every work type (issue type) id, cached for the session.

        Used only to express "visible everywhere except these work types" as
        ``restrictedToWorkTypes``. Returns an empty set when the lookup
        fails; the caller then keeps the field unrestricted instead of
        hiding it from work types it should still see.
        """
        if self._work_type_ids_cache is not None:
            return self._work_type_ids_cache
        try:
            raw = await self.client.get_json(f"{self.api_root}/issuetype")
        except Exception:  # noqa: BLE001 - best-effort restriction computation
            return frozenset()
        if not isinstance(raw, list):
            return frozenset()
        ids = {str(entry["id"]) for entry in raw if isinstance(entry, dict) and entry.get("id") is not None}
        self._work_type_ids_cache = frozenset(ids)
        return self._work_type_ids_cache

    async def create_field_scheme(
        self,
        *,
        name: str,
        description: str = "",
    ) -> str:
        """POST /config/fieldschemes. Returns the new scheme id."""
        result = await self.client.post_json(
            self._field_schemes_path(),
            json={"name": name, "description": description},
        )
        return _expect_id(result, f"POST {self._field_schemes_path()}")

    async def update_field_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(self._field_schemes_path(scheme_id), json=body)

    async def delete_field_scheme(self, scheme_id: str) -> None:
        await self.client.delete(self._field_schemes_path(scheme_id))

    async def get_field_scheme_fields(
        self,
        scheme_id: str,
        *,
        field_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield field-association rows for one field scheme.

        Each row carries ``fieldId``, ``parameters`` (defaults across work
        types), ``restrictedToWorkTypes``, and ``workTypeParameters``.
        """
        extra_params = {"fieldId": field_ids} if field_ids else None
        async for entry in common.paginate(
            self.client,
            self._field_schemes_path(scheme_id, "fields"),
            extra_params=extra_params,
        ):
            yield entry

    async def associate_fields_with_field_scheme(
        self,
        scheme_id: str,
        *,
        field_ids: list[str],
        restricted_to_work_types: list[str] | None = None,
    ) -> None:
        """PUT /config/fieldschemes/fields.

        ``restricted_to_work_types`` is a list of Jira issue-type ids. When
        omitted the field is associated with every work type (unrestricted).
        """
        if not field_ids:
            return
        association: dict[str, Any] = {"schemeIds": [_as_api_int(scheme_id)]}
        if restricted_to_work_types:
            association["restrictedToWorkTypes"] = [_as_api_int(wt) for wt in restricted_to_work_types]
        body = {field_id: [association] for field_id in field_ids}
        for batch in _field_scheme_batches(body, self.FIELD_SCHEME_BATCH_SIZE):
            await self.client.put_json(self._field_schemes_path("fields"), json=batch)

    async def remove_fields_from_field_scheme(
        self,
        scheme_id: str,
        *,
        field_ids: list[str],
    ) -> None:
        """DELETE /config/fieldschemes/fields.

        Removing an association that is already absent is tolerated: Jira
        reports a per-field failure rather than a hard error, and a repeat
        apply must be a no-op.
        """
        if not field_ids:
            return
        body = {field_id: {"schemeIds": [_as_api_int(scheme_id)]} for field_id in field_ids}
        for batch in _field_scheme_batches(body, self.FIELD_SCHEME_BATCH_SIZE):
            try:
                await self.client.delete(self._field_schemes_path("fields"), json=batch)
            except TransportError as exc:
                if exc.status_code == 400 and "not associated" in str(exc).lower():
                    continue
                raise

    async def set_field_scheme_field_parameters(
        self,
        scheme_id: str,
        *,
        field_id: str,
        required: bool,
        description: str = "",
        work_type_parameters: list[dict[str, Any]] | None = None,
    ) -> None:
        """PUT /config/fieldschemes/fields/parameters.

        ``work_type_parameters`` entries are ``{"workTypeId", "isRequired",
        "description"?}`` and override the default parameters for one work
        type.
        """
        parameters: dict[str, Any] = {"isRequired": required}
        if description:
            parameters["description"] = description
        entry: dict[str, Any] = {
            "schemeIds": [_as_api_int(scheme_id)],
            "parameters": parameters,
        }
        if work_type_parameters:
            entry["workTypeParameters"] = work_type_parameters
        await self.client.put_json(
            self._field_schemes_path("fields", "parameters"),
            json={field_id: [entry]},
        )

    async def associate_projects_with_field_scheme(
        self,
        scheme_id: str,
        *,
        project_ids: list[str],
    ) -> None:
        """PUT /config/fieldschemes/projects: bind a scheme to projects."""
        if not project_ids:
            return
        body = {
            str(_as_api_int(scheme_id)): {
                "projectIds": [_as_api_int(pid) for pid in project_ids],
            }
        }
        await self.client.put_json(self._field_schemes_path("projects"), json=body)

    async def get_projects_with_field_schemes(
        self,
        *,
        project_ids: list[str],
    ) -> dict[str, str]:
        """project_id → scheme_id lookup for the supplied projects."""
        if not project_ids:
            return {}
        out: dict[str, str] = {}
        async for entry in common.paginate(
            self.client,
            self._field_schemes_path("projects"),
            extra_params={"projectId": [_as_api_int(pid) for pid in project_ids]},
        ):
            project_id = entry.get("projectId")
            scheme_id = entry.get("schemeId")
            if project_id is not None and scheme_id is not None:
                out[str(project_id)] = str(scheme_id)
        return out

    async def search_field_scheme_projects(self, scheme_id: str) -> list[str]:
        """Project ids currently associated with one field scheme."""
        project_ids: list[str] = []
        async for entry in common.paginate(
            self.client,
            self._field_schemes_path(scheme_id, "projects"),
        ):
            project_id = entry.get("projectId") or entry.get("id")
            if project_id is not None:
                project_ids.append(str(project_id))
        return project_ids

    async def _set_field_scheme_mappings(
        self,
        scheme_id: str,
        *,
        mappings: list[dict[str, str]],
    ) -> None:
        """Translate a classic FCS mapping table onto one v2 field scheme.

        A classic FCS maps ``issueType → fieldConfiguration``. v2 has no
        scheme-of-configurations layer: one scheme contains the fields, with
        ``restrictedToWorkTypes`` expressing which work types see each field
        and ``workTypeParameters`` expressing per-work-type required flags.

        Visibility is computed per mapping, not per source scheme: a field in
        the default configuration stays visible to every work type that does
        not override it, and a field omitted from an overriding configuration
        is hidden for that work type only. When that "all except" set is
        needed, ``GET /issuetype`` supplies the full work-type list; if the
        lookup fails the field stays unrestricted rather than being hidden
        from work types that should still see it.
        """
        source_cache: dict[str, dict[str, dict[str, Any]]] = {}

        async def fields_for(fc_id: str) -> dict[str, dict[str, Any]]:
            fields = source_cache.get(fc_id)
            if fields is None:
                fields = {
                    str(field["fieldId"]): field
                    async for field in self.get_field_scheme_fields(fc_id)
                    if field.get("fieldId")
                }
                source_cache[fc_id] = fields
            return fields

        default_fc_id: str | None = None
        by_work_type: dict[str, str] = {}
        for mapping in mappings:
            issue_type_id = str(mapping.get("issueTypeId", "default"))
            fc_id = mapping.get("fieldConfigurationId")
            if not fc_id:
                continue
            if issue_type_id == "default":
                default_fc_id = str(fc_id)
            else:
                by_work_type[issue_type_id] = str(fc_id)

        default_fields = await fields_for(default_fc_id) if default_fc_id is not None else {}
        per_work_type_fields = {wt: await fields_for(fc_id) for wt, fc_id in by_work_type.items()}

        # A default field missing from an overriding configuration is the
        # only case that needs the full work-type list to express "visible
        # everywhere except X". Avoid the extra request otherwise.
        needs_all_work_types = any(
            field_id not in per_work_type_fields[work_type]
            for field_id in default_fields
            for work_type in per_work_type_fields
        )
        all_work_types: frozenset[str] = frozenset()
        if needs_all_work_types:
            known_work_types = await self._all_work_type_ids()
            if known_work_types:
                all_work_types = known_work_types | frozenset(by_work_type)

        # field id -> None (all work types) or set of restricted work-type ids
        visibility: dict[str, set[str] | None] = {}
        default_required: dict[str, bool] = {}
        # field id -> {work type id: required}
        worktype_required: dict[str, dict[str, bool]] = {}

        all_field_ids = set(default_fields) | {
            field_id for fields in per_work_type_fields.values() for field_id in fields
        }
        for field_id in all_field_ids:
            # Work types whose overriding configuration includes the field.
            specific_visible = {work_type for work_type, fields in per_work_type_fields.items() if field_id in fields}
            if field_id in default_fields:
                excluded = set(per_work_type_fields) - specific_visible
                if not excluded:
                    visible: set[str] | None = None
                elif all_work_types:
                    visible = set(all_work_types) - excluded
                else:
                    # Cannot express "all except these" without the full
                    # list; prefer a visible field over a wrongly hidden one.
                    visible = None
                default_required[field_id] = _field_scheme_required(default_fields[field_id])
            else:
                visible = specific_visible

            if visible == set():
                # Hidden for every mapped work type: leave it unassociated.
                continue
            visibility[field_id] = visible

        for work_type, fields in per_work_type_fields.items():
            for field_id, field in fields.items():
                if field_id in visibility:
                    worktype_required.setdefault(field_id, {})[work_type] = _field_scheme_required(field)

        # Remove associations the target scheme currently has but no longer
        # wants. This is also how a field that became hidden disappears.
        current_field_ids = {
            str(field["fieldId"]) async for field in self.get_field_scheme_fields(scheme_id) if field.get("fieldId")
        }
        stale = sorted(current_field_ids - set(visibility))
        if stale:
            await self.remove_fields_from_field_scheme(scheme_id, field_ids=stale)

        if not visibility:
            return

        # Associate every desired field, restricted where it is only visible
        # to specific work types.
        association_body: dict[str, Any] = {}
        for field_id, restricted in visibility.items():
            entry: dict[str, Any] = {"schemeIds": [_as_api_int(scheme_id)]}
            if restricted:
                entry["restrictedToWorkTypes"] = sorted(_as_api_int(wt) for wt in restricted)
            association_body[field_id] = [entry]
        for batch in _field_scheme_batches(association_body, self.FIELD_SCHEME_BATCH_SIZE):
            await self.client.put_json(self._field_schemes_path("fields"), json=batch)

        # Set required flags: defaults from the "default" mapping, then
        # per-work-type overrides from the specific mappings.
        parameter_body: dict[str, Any] = {}
        for field_id in visibility:
            entry = {
                "schemeIds": [_as_api_int(scheme_id)],
                "parameters": {"isRequired": default_required.get(field_id, False)},
            }
            overrides = worktype_required.get(field_id) or {}
            if overrides:
                entry["workTypeParameters"] = [
                    {"workTypeId": _as_api_int(wt), "isRequired": required}
                    for wt, required in sorted(overrides.items())
                ]
            parameter_body[field_id] = [entry]
        for batch in _field_scheme_batches(parameter_body, self.FIELD_SCHEME_BATCH_SIZE):
            await self.client.put_json(
                self._field_schemes_path("fields", "parameters"),
                json=batch,
            )

    # ── Issue types ──────────────────────────────────────────────────
    async def find_issuetype_id_by_name(self, name: str) -> str | None:
        """Return the id of an existing issue type with this exact name, else None.

        Only global issue types are considered. Names are unique among global
        types, but a tenant with team-managed projects also exposes same-named
        project-scoped types in ``/issuetype``; those cannot be managed through
        the global endpoints, so matching one would 400 at apply time. (#8)
        """
        for it in (await self._reflect_issuetypes()).values():
            if it.name == name and not it.project_scoped:
                return it.id
        return None

    async def create_issuetype(
        self,
        *,
        name: str,
        description: str = "",
        subtask: bool = False,
    ) -> str:
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "type": "subtask" if subtask else "standard",
        }
        result = await self.client.post_json(f"{self.api_root}/issuetype", json=body)
        return _expect_id(result, f"POST {self.api_root}/issuetype")

    async def delete_issuetype(self, issuetype_id: str) -> None:
        await self.client.delete(f"{self.api_root}/issuetype/{issuetype_id}")

    async def update_issuetype(
        self,
        issuetype_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(
            f"{self.api_root}/issuetype/{issuetype_id}",
            json=body,
        )

    # ── Projects ─────────────────────────────────────────────────────
    async def resolve_lead(self, lead: str) -> str:
        """Resolve a project-lead email to the platform user identifier.

        Schemas declare ``__lead__`` as an email; Cloud resolves it to an
        ``accountId``. A value without an ``@`` is assumed already-resolved
        and passed through unchanged, so schemas that still set a raw
        accountId keep working.

        Resolution calls ``GET {api_root}/user/search``, which requires the
        "Browse users and groups" global permission. A 403 there surfaces as
        a ConfigurationError with guidance rather than a raw transport error.
        Results are cached per engine run.
        """
        if "@" not in lead:
            return lead
        cached = self._lead_cache.get(lead)
        if cached is not None:
            return cached
        try:
            users = await self.client.get_json(
                f"{self.api_root}/user/search",
                params={self.user_search_param: lead},
            )
        except PermissionError as exc:
            raise ConfigurationError(
                f"Cannot resolve project lead {lead!r}: the API token lacks the "
                f"'Browse users and groups' permission required for user search. "
                f"Grant it, or set __lead__ to a resolved "
                f"{self.user_search_id_field} directly."
            ) from exc
        resolved = self._pick_lead_id(lead, users)
        self._lead_cache[lead] = resolved
        return resolved

    def _pick_lead_id(self, lead: str, users: Any) -> str:
        """Select the user id for ``lead`` from a user-search result list.

        Prefers an exact (case-insensitive) ``emailAddress`` match; falls back
        to the sole result when email is hidden (GDPR strips it on Cloud).
        Raises ConfigurationError when nothing usable is found.
        """
        if not isinstance(users, list) or not users:
            raise ConfigurationError(f"No Jira user found for project lead email {lead!r}.")
        matches = [u for u in users if str(u.get("emailAddress", "")).lower() == lead.lower()]
        if matches:
            chosen = matches[0]
        elif len(users) == 1:
            chosen = users[0]
        else:
            raise ConfigurationError(
                f"Project lead email {lead!r} matched {len(users)} users but none "
                f"by exact email; cannot disambiguate. Set __lead__ to a resolved "
                f"{self.user_search_id_field} directly."
            )
        user_id = chosen.get(self.user_search_id_field)
        if not user_id:
            raise ConfigurationError(
                f"Jira user for {lead!r} has no {self.user_search_id_field}; cannot use as project lead."
            )
        return str(user_id)

    async def create_project(
        self,
        *,
        key: str,
        name: str,
        project_type_key: str,
        lead: str,
        description: str = "",
        project_template_key: str | None = None,
    ) -> str:
        """Cloud takes ``leadAccountId``, not ``lead`` (username)."""
        body: dict[str, Any] = {
            "key": key,
            "name": name,
            "projectTypeKey": project_type_key,
            "leadAccountId": lead,
            "description": description,
        }
        if project_template_key:
            body["projectTemplateKey"] = project_template_key
        result = await self.client.post_json(f"{self.api_root}/project", json=body)
        return _expect_id(result, f"POST {self.api_root}/project")

    async def delete_project(self, *, project_id: str, project_key: str) -> None:
        """Cloud's delete-by-id is the safer call."""
        await self.client.delete(f"{self.api_root}/project/{project_id}")

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        lead: str | None = None,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if lead is not None:
            body["leadAccountId"] = lead
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(
            f"{self.api_root}/project/{project_id}",
            json=body,
        )

    # ── Search (M5 data plane) ───────────────────────────────────────
    async def search(
        self,
        *,
        jql: str,
        fields: list[str],
        page_size: int = 50,
    ):
        """Cloud's newer search uses POST /search/jql with nextPageToken pagination."""
        next_token: str | None = None
        while True:
            body_in: dict[str, Any] = {
                "jql": jql,
                "fields": fields if fields else ["*all"],
                "maxResults": page_size,
            }
            if next_token:
                body_in["nextPageToken"] = next_token
            body = await self.client.post_json(
                f"{self.api_root}/search/jql",
                json=body_in,
            )
            issues = body.get("issues", []) if isinstance(body, dict) else []
            for issue in issues:
                yield issue
            next_token = (body or {}).get("nextPageToken")
            if not next_token or not issues:
                return

    async def get_issue(self, key: str, *, fields: list[str]) -> dict:
        """Fetch one issue by key. Used by session.get(Model, key)."""
        params = {"fields": ",".join(fields) if fields else "*all"}
        return await self.client.get_json(
            f"{self.api_root}/issue/{key}",
            params=params,
        )

    # ── Issue writes (M6 data plane) ─────────────────────────────────
    async def create_issue(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /issue with ``{"fields": ...}``. Returns ``{"id":..., "key":..., "self":...}``."""
        return await self.client.post_json(f"{self.api_root}/issue", json=body)

    async def update_issue(self, key: str, body: dict[str, Any]) -> None:
        """PUT /issue/{key}. Body is ``{"fields": ...}`` with dirty fields only."""
        await self.client.put_json(f"{self.api_root}/issue/{key}", json=body)

    async def delete_issue(self, key: str) -> None:
        await self.client.delete(f"{self.api_root}/issue/{key}")

    # ── Issue security schemes ────────────────────────────────────────────

    async def _reflect_issue_security_schemes(self) -> dict[str, SecuritySchemeSnapshot]:
        """Reflect all issue security schemes from the Jira instance.

        Returns a dict mapping scheme ID -> SecuritySchemeSnapshot.
        """
        result: dict[str, SecuritySchemeSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/issuesecurityschemes"):
            scheme = common.parse_issue_security_scheme(entry)
            result[scheme.id] = scheme
        return result

    async def _reflect_issue_security_levels(self, scheme_id: str) -> dict[str, SecurityLevelSnapshot]:
        """Reflect all security levels for a given issue security scheme.

        Returns a dict mapping level ID -> SecurityLevelSnapshot.
        """
        result: dict[str, SecurityLevelSnapshot] = {}
        # Query: GET /rest/api/3/issuesecurityschemes/level?id=<scheme_id>
        payload = await self.client.get_json(
            f"{self.api_root}/issuesecurityschemes/level",
            params={"id": [scheme_id]},
        )
        for level in payload.get("values", []):
            members: list[SecurityLevelMemberSnapshot] = []
            for m in level.get("members", []):
                holder = m.get("holder", {})
                m_type = holder.get("type", "unknown")
                members.append(
                    SecurityLevelMemberSnapshot(
                        id=str(m.get("id", "")),
                        type=m_type,
                        name=str(holder.get("name", "")),
                    )
                )
            result[level["id"]] = SecurityLevelSnapshot(
                id=str(level["id"]),
                name=str(level["name"]),
                description=str(level.get("description", "")),
                is_default=level.get("isDefault", False),
                members=tuple(members),
            )
        return result

    async def _reflect_issue_security_level_members(
        self,
        scheme_id: str,
        level_id: str,
    ) -> dict[str, SecurityLevelMemberSnapshot]:
        """Reflect all members for a given security level.

        Returns a dict mapping member ID -> SecurityLevelMemberSnapshot.
        """
        result: dict[str, SecurityLevelMemberSnapshot] = {}
        payload = await self.client.get_json(
            f"{self.api_root}/issuesecurityschemes/level/member",
            params={"schemeId": scheme_id, "levelId": level_id},
        )
        for member in payload.get("values", []):
            holder = member.get("holder", {})
            m_type = holder.get("type", "unknown")
            result[member.get("id", "")] = SecurityLevelMemberSnapshot(
                id=str(member.get("id", "")),
                type=m_type,
                name=str(holder.get("name", "")),
            )
        return result

    async def create_issue_security_scheme(
        self,
        *,
        name: str,
        description: str = "",
        levels: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a new issue security scheme.

        Returns the new scheme's ID.

        **Permissions required:** Administer Jira global permission.

        **OAuth scopes:** `manage:jira-configuration`
        """
        body: dict[str, Any] = {"name": name, "description": description}
        if levels:
            body["levels"] = levels
        result = await self.client.post_json(f"{self.api_root}/issuesecurityschemes", json=body)
        return str(result["id"])

    async def update_issue_security_scheme(
        self, scheme_id: str, *, name: str | None = None, description: str | None = None
    ) -> None:
        """Update an existing issue security scheme's name and/or description.

        **Permissions required:** Administer Jira global permission.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        await self.client.put_json(f"{self.api_root}/issuesecurityschemes/{scheme_id}", json=body)

    async def delete_issue_security_scheme(self, scheme_id: str) -> None:
        """Delete an issue security scheme.

        **Permissions required:** Administer Jira global permission.

        The scheme must not be associated with any project, or the call will fail.
        """
        await self.client.delete(f"{self.api_root}/issuesecurityschemes/{scheme_id}")

    async def associate_security_scheme_to_project(
        self,
        *,
        project_id: str,
        scheme_id: str,
        old_to_new_level_mappings: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Associate an issue security scheme with a project.

        This is an asynchronous operation; the response includes a `location` link
        to check task progress.

        **Permissions required:** Administer Jira global permission.

        **OAuth scopes:** `manage:jira-configuration`

        :param project_id: The Jira project ID.
        :param scheme_id: The issue security scheme ID to associate.
        :param old_to_new_level_mappings: Optional mapping of old level IDs to new level IDs
            for remapping existing issues' security levels.
        :return: Task progress dict.
        """
        body: dict[str, Any] = {"projectId": project_id, "schemeId": scheme_id}
        if old_to_new_level_mappings:
            body["oldToNewSecurityLevelMappings"] = old_to_new_level_mappings
        result = await self.client.put_json(f"{self.api_root}/issuesecurityschemes/project", json=body)
        return result

    async def disassociate_security_scheme_from_project(self, project_id: str) -> None:
        """Disassociate the issue security scheme from a project, falling back to Jira's default.

        **Permissions required:** Administer Jira global permission.
        """
        await self.client.put_json(
            f"{self.api_root}/issuesecurityschemes/project",
            json={"projectId": project_id},
        )

    # ── Issue security level member operations ────────────────────────────

    async def add_issue_security_level_member(
        self,
        *,
        scheme_id: str,
        level_id: str,
        member_type: str,  # "user" or "group"
        member_id_or_name: str,  # accountId for user, group name for group
    ) -> None:
        """Add a user or group as a member of a security level.

        **Permissions required:** Administer Jira global permission.

        **OAuth scopes:** `manage:jira-configuration`
        """
        if member_type == "user":
            # Add user by accountId
            body = {"members": [{"parameter": member_id_or_name, "type": "user"}]}
        elif member_type == "group":
            # Add group by group name
            body = {"members": [{"parameter": member_id_or_name, "type": "group"}]}
        else:
            raise ValueError(f"Invalid member_type: {member_type!r}. Expected 'user' or 'group'.")
        await self.client.put_json(
            f"{self.api_root}/issuesecurityschemes/{scheme_id}/level/{level_id}/member",
            json=body,
        )

    async def remove_issue_security_level_member(
        self,
        *,
        scheme_id: str,
        level_id: str,
        member_id: str,
    ) -> None:
        """Remove a member from a security level.

        **Permissions required:** Administer Jira global permission.
        """
        await self.client.delete(
            f"{self.api_root}/issuesecurityschemes/{scheme_id}/level/{level_id}/member/{member_id}"
        )


def _expect_id(result: Any, where: str) -> str:
    if not isinstance(result, dict) or "id" not in result:
        raise ReflectionError(f"{where} returned no id: {result!r}")
    return str(result["id"])


_RETIRED_FC_HINTS = ("Cannot create a new field configuration", "use Field Scheme instead")


def _is_retired_field_configuration_error(exc: TransportError) -> bool:
    """True for the specific 400 Jira returns for the retired classic API."""
    if exc.status_code != 400 or not exc.args or not isinstance(exc.args[0], str):
        return False
    body = exc.args[0]
    return all(hint in body for hint in _RETIRED_FC_HINTS)


def _maybe_wrap_retired_field_configuration(exc: TransportError) -> BaseException:
    """Translate Jira Cloud's retired-field-configuration 400 into a
    ConfigurationError, with a pointer to the replacement Field Schemes API.

    Atlassian retired classic field configurations on Jira Cloud in May
    2026 (RFC-103/104/105 define the replacement Field Schemes API). The
    classic endpoints return 400 with ``Cannot create a new field
    configuration. Please use Field Scheme instead.`` rather than 410 or
    404, so this is a content-shape check, not a status-code one. Any
    other 4xx keeps the original TransportError so the caller still sees
    a real wire error.

    ``create_field_configuration`` already re-probes and retries on the v2
    Field Schemes API before reaching this wrapper. This message is the
    last resort for a tenant where the v2 probe also fails.
    """
    if not _is_retired_field_configuration_error(exc):
        return exc
    body = exc.args[0]
    return ConfigurationError(
        "Jira Cloud retired classic field configurations in May 2026: "
        f"{body[:200]}. "
        "Use the Field Schemes API instead (see Atlassian's RFC-103/104/105). "
        "stint probes /config/fieldschemes per tenant and routes "
        "automatically; a v2 probe also failed here, so check that the "
        "Field Schemes feature is enabled and the token can administer it."
    )


def _field_scheme_batches(body: dict[str, Any], size: int) -> list[dict[str, Any]]:
    """Split a field-keyed request body under the v2 per-request field cap."""
    items = list(body.items())
    return [dict(items[index : index + size]) for index in range(0, len(items), size)]


def _as_api_int(value: Any) -> Any:
    """Return ``value`` as an int when it is a numeric Jira id.

    Jira's v2 schemas type ids as int64. State stores them as strings, and a
    few tests use opaque ids; keep non-numeric values unchanged rather than
    raising so the dialect stays usable with mocks and older state.
    """
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return value


def _field_scheme_required(field: dict[str, Any]) -> bool:
    """Whether a v2 field-association row marks the field required.

    ``parameters`` is the default across work types; ``workTypeParameters``
    may set it for specific ones. The snapshot shape only carries one bool,
    so treat required on any work type as required.
    """
    parameters = field.get("parameters") or {}
    if parameters.get("isRequired"):
        return True
    return any((wt or {}).get("isRequired") for wt in field.get("workTypeParameters") or [])
