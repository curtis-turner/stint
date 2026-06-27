"""Shared Jira dialect base. DC and Cloud subclass this and override the bits
that diverge: ``api_root`` and the option-reflection path (Cloud uses field
contexts; DC hits /option directly).
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
    ServerInfoSnapshot,
    Snapshot,
)


class JiraDialectBase:
    """Shared reflection skeleton for DC and Cloud."""

    name: ClassVar[str] = "jira_base"
    api_root: ClassVar[str] = "/rest/api/2"
    expected_deployment_type: ClassVar[str] = "Server"
    # DC exposes field-configuration items at /fieldconfiguration/{id}/items;
    # Cloud serves the same data at /fieldconfiguration/{id}/fields.
    field_config_items_segment: ClassVar[str] = "items"
    # User-search divergence for lead resolution: DC matches on ``username``
    # and returns the user's ``name``; Cloud matches on ``query`` and returns
    # an ``accountId`` (see the Cloud override).
    user_search_param: ClassVar[str] = "username"
    user_search_id_field: ClassVar[str] = "name"

    def __init__(self, client: JiraHTTPClient) -> None:
        self.client = client
        self._lead_cache: dict[str, str] = {}

    # ── Detection ────────────────────────────────────────────────────
    async def detect(self) -> bool:
        info = await self._server_info()
        return info.deployment_type == self.expected_deployment_type

    async def _server_info(self) -> ServerInfoSnapshot:
        payload = await self.client.get_json(f"{self.api_root}/serverInfo")
        return ServerInfoSnapshot(
            deployment_type=str(payload.get("deploymentType", "")),
            version=str(payload.get("version", "")),
            base_url=str(payload.get("baseUrl", self.client.base_url)),
        )

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
        )

    # ── Custom fields ────────────────────────────────────────────────
    async def _iter_custom_field_payloads(self) -> AsyncIterator[dict[str, Any]]:
        """Yield raw field payloads to scan for custom fields.

        Base (DC): ``GET /field`` returns a bare list of every field. Cloud
        overrides this because ``GET /field`` there returns only a subset of
        custom fields; see the Cloud dialect for the paginated search. (#9)
        """
        raw_fields = await self.client.get_json(f"{self.api_root}/field")
        if not isinstance(raw_fields, list):
            raise ReflectionError(f"{self.api_root}/field returned non-list: {type(raw_fields)}")
        for entry in raw_fields:
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
        """Default DC behavior: GET /field/{id}/option (paginated).

        Cloud overrides this because option access lives under a field
        context.
        """
        items: list[dict] = []
        async for opt in common.paginate(self.client, f"{self.api_root}/field/{field_id}/option"):
            items.append(opt)
        return common.parse_field_options(items)

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
        fcs_map = await self._reflect_project_scheme_bindings(
            f"{self.api_root}/fieldconfigurationscheme/project",
            project_ids=project_ids,
            scheme_key="fieldConfigurationScheme",
        )
        for key, proj in list(result.items()):
            result[key] = dataclasses.replace(
                proj,
                issuetype_scheme_id=its_map.get(proj.id),
                issuetype_screen_scheme_id=itss_map.get(proj.id),
                field_configuration_scheme_id=fcs_map.get(proj.id),
            )
        return result

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
        scheme id). DC has the list endpoint but not /mapping, so on DC the
        members come from a different path — overridden in dialect subclasses
        if needed.
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

    async def _reflect_field_configuration_items(self, fc_id: str) -> dict[str, FieldConfigurationItemSnapshot]:
        items: dict[str, FieldConfigurationItemSnapshot] = {}
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/fieldconfiguration/{fc_id}/{self.field_config_items_segment}",
        ):
            item = common.parse_field_configuration_item(entry)
            if item.field_id:
                items[item.field_id] = item
        return items

    # ── Field configuration schemes ──────────────────────────────────
    async def _reflect_field_configuration_schemes(
        self,
    ) -> dict[str, FieldConfigurationSchemeSnapshot]:
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
        return str(result["id"])

    async def add_custom_field_option(self, field_id: str, value: str) -> str:
        """POST /field/{id}/option. Returns the new option id.

        DC default. Cloud overrides because options live under field contexts.
        """
        result = await self.client.post_json(
            f"{self.api_root}/field/{field_id}/option",
            json={"value": value},
        )
        if not isinstance(result, dict) or "id" not in result:
            raise ReflectionError(f"POST /field/{field_id}/option returned no id: {result!r}")
        return str(result["id"])

    async def delete_custom_field(self, field_id: str) -> None:
        await self.client.delete(f"{self.api_root}/field/{field_id}")

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
        """DC: DELETE /field/{id}/option/{optId}. Cloud overrides."""
        await self.client.delete(f"{self.api_root}/field/{field_id}/option/{option_id}")

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

    # ── Field configurations ─────────────────────────────────────────
    async def create_field_configuration(
        self,
        *,
        name: str,
        description: str = "",
    ) -> str:
        body: dict[str, Any] = {"name": name, "description": description}
        result = await self.client.post_json(
            f"{self.api_root}/fieldconfiguration",
            json=body,
        )
        return _expect_id(result, f"POST {self.api_root}/fieldconfiguration")

    async def delete_field_configuration(self, fc_id: str) -> None:
        await self.client.delete(f"{self.api_root}/fieldconfiguration/{fc_id}")

    async def set_field_configuration_item(
        self,
        fc_id: str,
        *,
        field_id: str,
        required: bool = False,
        hidden: bool = False,
        description: str = "",
    ) -> None:
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

    # ── Field configuration schemes ──────────────────────────────────
    async def create_field_configuration_scheme(
        self,
        *,
        name: str,
        description: str = "",
    ) -> str:
        body: dict[str, Any] = {"name": name, "description": description}
        result = await self.client.post_json(
            f"{self.api_root}/fieldconfigurationscheme",
            json=body,
        )
        return _expect_id(result, f"POST {self.api_root}/fieldconfigurationscheme")

    async def delete_field_configuration_scheme(self, scheme_id: str) -> None:
        await self.client.delete(f"{self.api_root}/fieldconfigurationscheme/{scheme_id}")

    async def update_field_configuration_scheme(
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
        await self.client.put_json(
            f"{self.api_root}/fieldconfigurationscheme/project",
            json={"fieldConfigurationSchemeId": scheme_id, "projectId": project_id},
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

        Schemas declare ``__lead__`` as an email. DC resolves it to a
        username; Cloud resolves it to an ``accountId`` (see the override).
        A value without an ``@`` is assumed already-resolved and passed
        through unchanged, so schemas that still set a raw username/accountId
        keep working.

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
        """DC takes a username as ``lead``. Cloud overrides to send ``leadAccountId``."""
        body: dict[str, Any] = {
            "key": key,
            "name": name,
            "projectTypeKey": project_type_key,
            "lead": lead,
            "description": description,
        }
        if project_template_key:
            body["projectTemplateKey"] = project_template_key
        result = await self.client.post_json(f"{self.api_root}/project", json=body)
        return _expect_id(result, f"POST {self.api_root}/project")

    async def delete_project(self, *, project_id: str, project_key: str) -> None:
        """DC accepts either id or key; Cloud requires id. Both passed."""
        await self.client.delete(f"{self.api_root}/project/{project_key}")

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        lead: str | None = None,
        description: str | None = None,
    ) -> None:
        """DC accepts lead as username. Cloud overrides to send leadAccountId."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if lead is not None:
            body["lead"] = lead
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
        """Yield issue payloads matching `jql`. DC: legacy GET /search.

        Returns an async iterator of raw issue dicts. Caller (the session)
        hydrates them into model instances.
        """
        start = 0
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "fields": ",".join(fields) if fields else "*all",
                "startAt": start,
                "maxResults": page_size,
            }
            body = await self.client.get_json(
                f"{self.api_root}/search",
                params=params,
            )
            issues = body.get("issues", []) if isinstance(body, dict) else []
            for issue in issues:
                yield issue
            if not issues:
                return
            total = body.get("total", 0)
            start += len(issues)
            if start >= total:
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


def _expect_id(result: Any, where: str) -> str:
    if not isinstance(result, dict) or "id" not in result:
        raise ReflectionError(f"{where} returned no id: {result!r}")
    return str(result["id"])
