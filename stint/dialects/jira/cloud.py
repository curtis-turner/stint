"""Jira Cloud dialect for company-managed projects.

Team-managed writes live in the separate ``stint.dialects.jira.tmp`` dialect.
"""

from __future__ import annotations

import dataclasses
import json
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

from stint.client.http import JiraHTTPClient
from stint.dialects.jira import common
from stint.exceptions import (
    ConfigurationError,
    NotFoundError,
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
    PermissionSchemeSnapshot,
    ProjectSnapshot,
    RoleActorSnapshot,
    ScreenSchemeSnapshot,
    ScreenSnapshot,
    ScreenTabSnapshot,
    SecurityLevelMemberSnapshot,
    SecurityLevelSnapshot,
    SecuritySchemeSnapshot,
    ServerInfoSnapshot,
    Snapshot,
    StatusSnapshot,
    WorkflowSchemeSnapshot,
    WorkflowSnapshot,
    WorkflowStatusSnapshot,
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
    # Attachment upload CSRF guard: Jira 403s the multipart POST without it.
    ATTACHMENT_TOKEN: ClassVar[str] = "no-check"

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
    # Endpoints taking a repeated ``projectId`` query filter cap it around
    # 100 ids per request; beyond that they 400 (all reflection fails) or
    # silently truncate (missing bindings fabricate scheme drift). (M10)
    PROJECT_FILTER_BATCH_SIZE: ClassVar[int] = 50

    def __init__(self, client: JiraHTTPClient) -> None:
        self.client = client
        self._lead_cache: dict[str, str] = {}
        self._user_cache: dict[str, str] = {}
        self._field_config_api_cache: str | None = None
        self._all_field_ids_cache: frozenset[str] | None = None
        self._work_type_ids_cache: frozenset[str] | None = None
        # Lockout-guard facts, cached for the life of the dialect instance.
        self._my_account_id_cache: str | None = None
        self._user_groups_cache: dict[str, frozenset[str]] = {}
        self._role_membership_cache: dict[tuple[str, str], set[str]] = {}
        self._global_roles_cache: dict[str, str] | None = None

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
        if not isinstance(payload, dict):
            # An empty body or a non-object payload (proxy, captive portal)
            # must raise a StintError, not an AttributeError. (M16)
            raise ReflectionError(
                f"{self.api_root}/serverInfo returned {type(payload).__name__} instead of a JSON object"
            )
        return ServerInfoSnapshot(
            deployment_type=str(payload.get("deploymentType", "")),
            version=str(payload.get("version", "")),
            base_url=str(payload.get("baseUrl", self.client.base_url)),
        )

    # ── Field configuration API capability detection ─────────────────
    async def field_config_api(self, *, force: bool = False) -> str:
        """Return which field-configuration API this tenant speaks.

        ``v2`` means Field Schemes (``/config/fieldschemes``), ``v1`` means
        the classic field-configuration endpoints. Only *positive*
        determinations are cached on the dialect instance (H6): ``v2`` on a
        successful probe, and ``v1`` only when the server's answer itself
        indicates the new API is absent (404/400). Any other failure —
        timeouts, connection errors, exhaustion of 429/503 retries, auth —
        returns ``v1`` for this call *without caching*, so the next call
        re-probes instead of pinning a genuine v2 tenant to retired v1
        endpoints. Pass ``force=True`` to re-probe after a retired-API
        error (``_probe_after_retired_error``).

        A failed probe still falls back to ``v1`` rather than raising:
        tenants that predate the Field Schemes feature flag answer
        404/400 here, and a transient failure should not make every
        field-configuration read fail. If v1 has actually been retired,
        the write paths detect the specific 400 and retry on v2 (see
        ``_retry_v2_after_retired``).
        """
        if self._field_config_api_cache is not None and not force:
            return self._field_config_api_cache
        try:
            await self.client.get_json(
                f"{self.api_root}/{self.FIELD_SCHEMES_SEGMENT}",
                params={"startAt": 0, "maxResults": 1},
            )
        except NotFoundError:
            # 404 = the tenant does not expose the Field Schemes API.
            version = self.FIELD_CONFIG_API_V1
            self._field_config_api_cache = version
        except TransportError as exc:
            if exc.status_code == 400:
                # A 400 from the probe itself is a server-side answer that
                # the API is not available here — cache v1 for this run.
                version = self.FIELD_CONFIG_API_V1
                self._field_config_api_cache = version
            else:
                # Transient/infra failure: answer v1 for this call only,
                # without caching, so the next call re-probes (H6).
                version = self.FIELD_CONFIG_API_V1
        except Exception:  # noqa: BLE001 - capability probes must never break a run
            # Same rule: never cache on unexpected failures.
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
        permission_schemes = await self._reflect_permission_schemes()
        project_roles = await self._reflect_global_project_roles()
        statuses, workflows = await self._reflect_workflows()
        workflow_schemes = await self._reflect_workflow_schemes()
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
            permission_schemes=permission_schemes,
            project_roles=project_roles,
            statuses=statuses,
            workflows=workflows,
            workflow_schemes=workflow_schemes,
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

        ``searcherKey`` is an *expandable* attribute (the OpenAPI lists it
        among ``expand`` options, next to ``key``/``lastUsed``/``screensCount``).
        Without listing it the listing never carries it, so every field
        reflected with an empty searcher and the diff re-emitted each declared
        searcher as a phantom update on every revision — even though Jira was
        in sync. The reflect request therefore expands ``searcherKey``.
        """
        async for entry in common.paginate(
            self.client,
            f"{self.api_root}/field/search",
            extra_params={"type": "custom", "expand": "searcherKey"},
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
                    # The rebuilt snapshot must carry the parsed description:
                    # dropping it made every select-style field report "" and
                    # diff re-emit a description update forever (the
                    # bug_severity phantom). Same for searcher_key.
                    description=cf.description,
                    searcher_key=cf.searcher_key,
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
        iss_map = await self._reflect_project_issue_security_bindings(project_ids)
        perm_map = await self._reflect_project_permission_scheme_bindings(project_ids)
        wf_scheme_map = await self._reflect_project_scheme_bindings(
            f"{self.api_root}/workflowscheme/project",
            project_ids=project_ids,
            scheme_key="workflowScheme",
        )
        roles_map: dict[str, dict[str, tuple[RoleActorSnapshot, ...]]] = {}
        for proj in list(result.values()):
            roles_map[proj.id] = await self._reflect_project_roles(proj.id)
        for key, proj in list(result.items()):
            result[key] = dataclasses.replace(
                proj,
                issuetype_scheme_id=its_map.get(proj.id),
                issuetype_screen_scheme_id=itss_map.get(proj.id),
                field_configuration_scheme_id=fcs_map.get(proj.id),
                issue_security_scheme_id=iss_map.get(proj.id),
                permission_scheme_id=perm_map.get(proj.id),
                workflow_scheme_id=wf_scheme_map.get(proj.id, ""),
                roles=roles_map.get(proj.id, {}),
            )
        return result

    @staticmethod
    def _chunked_ids(project_ids: list[str], size: int) -> list[list[str]]:
        """Split an id list into request-sized chunks for bulk filters."""
        return [project_ids[i : i + size] for i in range(0, len(project_ids), size)]

    async def _reflect_project_field_configuration_bindings(
        self,
        project_ids: list[str],
    ) -> dict[str, str]:
        """project_id → field-scheme/configuration id map, for whichever API.

        The repeated ``projectId`` filter is chunked (M10): one request with
        every id on the tenant 400s or silently truncates beyond Jira's cap.
        """
        if not project_ids:
            return {}
        if not await self._field_config_api_is_v2():
            return await self._reflect_project_scheme_bindings(
                f"{self.api_root}/fieldconfigurationscheme/project",
                project_ids=project_ids,
                scheme_key="fieldConfigurationScheme",
            )
        out: dict[str, str] = {}
        for chunk in self._chunked_ids(project_ids, self.PROJECT_FILTER_BATCH_SIZE):
            async for entry in common.paginate(
                self.client,
                f"{self.api_root}/{self.FIELD_SCHEMES_SEGMENT}/projects",
                extra_params={"projectId": [_as_api_int(pid) for pid in chunk]},
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

        The repeated ``projectId`` filter is chunked and the per-chunk maps
        merged (M10).
        """
        out: dict[str, str] = {}
        for chunk in self._chunked_ids(project_ids, self.PROJECT_FILTER_BATCH_SIZE):
            async for entry in common.paginate(
                self.client,
                path,
                extra_params={"projectId": chunk},
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
    async def _reflect_project_issue_security_bindings(
        self,
        project_ids: list[str],
    ) -> dict[str, str]:
        """project_id → issue-security-scheme id map.

        Entries without ``issueSecuritySchemeId`` are projects on the default
        (no explicit binding) and are skipped. The repeated ``projectId``
        filter is chunked (M10), same as the sibling bindings. IDs come back
        as ints; stringify for snapshot parity.
        """
        out: dict[str, str] = {}
        for chunk in self._chunked_ids(project_ids, self.PROJECT_FILTER_BATCH_SIZE):
            async for entry in common.paginate(
                self.client,
                f"{self.api_root}/issuesecurityschemes/project",
                extra_params={"projectId": chunk},
            ):
                scheme_id = entry.get("issueSecuritySchemeId")
                project_id = entry.get("projectId")
                if scheme_id is not None and project_id is not None:
                    out[str(project_id)] = str(scheme_id)
        return out

    async def _reflect_project_permission_scheme_bindings(
        self,
        project_ids: list[str],
    ) -> dict[str, str]:
        """project_id → permission-scheme id map.

        There is no bulk endpoint: one GET per project of
        ``/project/{id}/permissionscheme`` (which always answers — every
        project carries a scheme, Jira's default included). IDs come back as
        ints; stringify for snapshot parity.
        """
        out: dict[str, str] = {}
        for project_id in project_ids:
            payload = await self.client.get_json(f"{self.api_root}/project/{project_id}/permissionscheme")
            if isinstance(payload, dict) and payload.get("id") is not None:
                out[project_id] = str(payload["id"])
        return out

    async def _reflect_project_roles(self, project_id: str) -> dict[str, tuple[RoleActorSnapshot, ...]]:
        """role name → actors for one project.

        ``GET /project/{id}/role`` answers a role-name → URL map; each role's
        actors come from ``GET /project/{id}/role/{roleId}``. Note the two
        meanings of "role" here: this maps the *tenant's* project roles into
        one project, so role sets can differ per project.
        """
        roles = await self.client.get_json(f"{self.api_root}/project/{project_id}/role")
        if not isinstance(roles, dict):
            raise ReflectionError(
                f"GET {self.api_root}/project/{project_id}/role did not return "
                f"a role-name → URL mapping (got {type(roles).__name__})."
            )
        result: dict[str, tuple[RoleActorSnapshot, ...]] = {}
        for role_name, role_url in roles.items():
            role_id = str(role_url).rstrip("/").rsplit("/", 1)[-1]
            payload = await self.client.get_json(f"{self.api_root}/project/{project_id}/role/{role_id}")
            if not isinstance(payload, dict):
                continue
            result[str(role_name)] = common.parse_role_actors(payload.get("actors") or [])
        return result

    async def _reflect_permission_schemes(self) -> dict[str, PermissionSchemeSnapshot]:
        """Reflect all permission schemes with their grants.

        ``GET /permissionscheme`` answers a bare
        ``{"permissionSchemes": [...]}`` envelope with no pagination keys, so
        like ``/issuesecurityschemes`` it must not go through
        ``common.paginate``. Grants are only included with
        ``expand=permissions``; without it every scheme would reflect
        grant-less and all declared grants would diff as phantom drift.
        """
        payload = await self.client.get_json(
            f"{self.api_root}/permissionscheme",
            params={"expand": "permissions"},
        )
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/permissionscheme returned "
                f"{type(payload).__name__} instead of a permissionSchemes envelope"
            )
        result: dict[str, PermissionSchemeSnapshot] = {}
        for entry in payload.get("permissionSchemes") or []:
            if not isinstance(entry, dict):
                raise ReflectionError(
                    f"GET {self.api_root}/permissionscheme included a non-object scheme entry: {str(entry)[:200]!r}"
                )
            scheme = common.parse_permission_scheme(entry)
            result[scheme.id] = scheme
        return result

    async def _reflect_global_project_roles(self) -> dict[str, str]:
        """Global project-role name → id map (``GET /rest/api/3/role``).

        Used to compare permission-scheme grants whose holder is a
        projectRole: Jira's holder parameter is the role id while
        declarations name the role. Returns an empty map when the endpoint
        is unavailable (older/oddball tenants), which degrades grant-holder
        comparison to "cannot verify role holders" instead of inventing
        drift.
        """
        out: dict[str, str] = {}
        try:
            async for entry in common.paginate(self.client, f"{self.api_root}/role"):
                name, role_id = common.parse_global_role(entry)
                if name and role_id:
                    out[name] = role_id
        except NotFoundError:
            return {}
        return out

    async def _reflect_workflows(
        self,
    ) -> tuple[dict[str, StatusSnapshot], dict[str, WorkflowSnapshot]]:
        """Reflect tenant statuses and workflows (``GET /workflows/search``).

        The response carries two top-level arrays: ``values`` (the
        workflows, each referencing statuses by ``statusReference``) and
        ``statuses`` (the flattened tenant-level status objects). Both are
        needed: statuses get keyed by id here, workflows resolve their
        status references against them, and the tenant status map is keyed
        by name for the declared-status diff. Reflects all scopes; the
        declared/diff layers filter to GLOBAL (company-managed) workflows.

        Uses its own startAt page loop because ``common.paginate`` yields
        envelope items while this endpoint's page envelope carries the
        ``statuses`` array too — dropping pages would drop statuses used
        only by later-page workflows.
        """
        statuses_by_id: dict[str, WorkflowStatusSnapshot] = {}
        workflows: dict[str, WorkflowSnapshot] = {}
        start = 0
        page_size = 50
        for _ in range(common._MAX_PAGES):
            page = await self.client.get_json(
                f"{self.api_root}/workflows/search",
                params={"startAt": start, "maxResults": page_size},
            )
            if not isinstance(page, dict) or "values" not in page:
                raise ReflectionError(
                    f"GET {self.api_root}/workflows/search returned an unrecognized "
                    f"pagination envelope: {str(page)[:200]!r}"
                )
            statuses_by_id.update(common.parse_workflow_status_map(page.get("statuses") or []))
            for entry in page.get("values") or []:
                if isinstance(entry, dict):
                    workflow = common.parse_workflow(entry, statuses_by_id)
                    workflows[workflow.name] = workflow
            if page.get("isLast") is not False:
                break
            start += len(page.get("values") or []) or page_size
        else:
            raise ReflectionError(f"GET {self.api_root}/workflows/search exceeded {common._MAX_PAGES} pages")

        statuses: dict[str, StatusSnapshot] = {}
        for status in statuses_by_id.values():
            statuses.setdefault(
                status.name,
                StatusSnapshot(
                    id=status.id,
                    name=status.name,
                    category=status.category,
                    description=status.description,
                    scope=status.scope,
                ),
            )
        return statuses, workflows

    async def _reflect_workflow_schemes(self) -> dict[str, WorkflowSchemeSnapshot]:
        """Reflect all workflow schemes (``GET /workflowscheme``).

        Returns a paginated ``PageBeanWorkflowScheme`` of the classic
        shape (workflows referenced by name). Draft schemes are excluded
        by Jira; active-scheme edits go through the new APIs which have no
        draft concept.
        """
        schemes: dict[str, WorkflowSchemeSnapshot] = {}
        async for entry in common.paginate(self.client, f"{self.api_root}/workflowscheme"):
            if isinstance(entry, dict):
                scheme = common.parse_workflow_scheme(entry)
                schemes[scheme.id] = scheme
        return schemes

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
            # Summarize: a busy tenant has dozens of these, and listing every
            # id on one line buries the rest of reflection's output.
            shown = ", ".join(skipped[:3])
            more = "" if len(skipped) <= 3 else f" (+{len(skipped) - 3} more)"
            warnings.warn(
                f"reflection skipped {len(skipped)} team-managed synthetic "
                f"screen(s) (their /tabs endpoint returns 400; not "
                f"stint-managed): {shown}{more}",
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
        # The API does not guarantee context ordering and the default
        # context is not necessarily first; the global context is the one
        # that applies to every issue, so prefer it explicitly and fall
        # back to the first entry otherwise.
        for ctx in contexts:
            if ctx.get("isGlobalContext") is True:
                return str(ctx.get("id", "")) or None
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
        try:
            await self.client.post_json(
                f"{self.api_root}/screens/{screen_id}/tabs/{tab_id}/fields",
                json={"fieldId": field_id},
            )
        except TransportError as exc:
            # Re-running a partially applied migration hits this exact 400
            # for every field the first run already added ("The field with id
            # summary already exists on the screen."). The field is on the
            # tab, which is the declared state: treat it as done so resume
            # picks up where it left off instead of failing on the first
            # already-applied field.
            if exc.status_code == 400 and "already exists on the screen" in _transport_body(exc):
                return
            raise

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

    async def set_issuetype_scheme_issuetypes(
        self,
        scheme_id: str,
        *,
        issuetype_ids: list[str],
    ) -> None:
        """Reconcile the scheme's member issuetype list to ``issuetype_ids``.

        Cloud has no single "replace members" call, so this composes three
        primitives:
          - ``PUT /issuetypescheme/{id}/issuetype`` adds missing members. The
            endpoint appends to the scheme and rejects any id it already
            contains, so only absent ids are sent.
          - ``DELETE /issuetypescheme/{id}/issuetype/{issueTypeId}`` removes
            each surplus member. Jira 400s removals it cannot do (member in
            use by issues, last standard member, members of the global
            default scheme); that surfaces as a TransportError rather than
            being swallowed.
          - ``PUT /issuetypescheme/{id}/issuetype/move`` fixes the ordering.
        """
        desired = list(dict.fromkeys(issuetype_ids))
        current = await self._reflect_issuetype_scheme_members(scheme_id)
        to_add = [i for i in desired if i not in set(current)]
        to_remove = [i for i in current if i not in set(desired)]
        if to_add:
            await self.client.put_json(
                f"{self.api_root}/issuetypescheme/{scheme_id}/issuetype",
                json={"issueTypeIds": to_add},
            )
        for issue_type_id in to_remove:
            await self.client.delete(f"{self.api_root}/issuetypescheme/{scheme_id}/issuetype/{issue_type_id}")
        # Expected server order after the mutations: survivors keep their
        # relative order, adds are appended in the order they were sent.
        members = [i for i in current if i not in to_remove] + to_add
        if members == desired:
            return
        await self._reorder_issuetype_scheme_members(scheme_id, members, desired)

    @staticmethod
    def _sim_move(
        members: list[str],
        issue_type_id: str,
        *,
        after: str | None = None,
        position: str | None = None,
    ) -> list[str]:
        """Mirror of the documented effect of one ``/issuetype/move`` call:
        take ``issue_type_id`` out and insert it after ``after`` (or at
        ``position``). Used to decide whether a move is still needed."""
        rest = [i for i in members if i != issue_type_id]
        if position == "First":
            index = 0
        elif position == "Last":
            index = len(rest)
        else:
            index = rest.index(after or "") + 1
        rest.insert(index, issue_type_id)
        return rest

    async def _reorder_issuetype_scheme_members(
        self,
        scheme_id: str,
        current: list[str],
        desired: list[str],
    ) -> None:
        """Issue ``/issuetype/move`` calls until members match ``desired``.

        The move endpoint places the listed ids directly after ``after`` (or
        at ``position``), keeping their relative order. Fix the head with a
        First move, then walk the tail: each out-of-place member moves to
        directly follow its desired predecessor.
        """
        if current[0] != desired[0]:
            await self.client.put_json(
                f"{self.api_root}/issuetypescheme/{scheme_id}/issuetype/move",
                json={"issueTypeIds": [desired[0]], "position": "First"},
            )
            current = self._sim_move(current, desired[0], position="First")
        for i in range(1, len(desired)):
            member = desired[i]
            if i < len(current) and current[i] == member:
                continue
            anchor = desired[i - 1]
            await self.client.put_json(
                f"{self.api_root}/issuetypescheme/{scheme_id}/issuetype/move",
                json={"issueTypeIds": [member], "after": anchor},
            )
            current = self._sim_move(current, member, after=anchor)
        if current != desired:
            raise ReflectionError(
                f"reordering issuetype scheme {scheme_id} did not converge: expected {desired}, computed {current}"
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

    async def _retry_v2_after_retired(
        self,
        exc: TransportError,
        v2: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Shared tail for a classic (v1) field-configuration write rejected
        by the retired-API 400 (M15): re-probe; when the tenant actually
        speaks Field Schemes, redo the write there; otherwise surface the
        actionable retirement error. Every v1 write gets this, not just
        create — a bare ``TransportError`` mid-apply leaves earlier steps
        unrolled back.
        """
        if _is_retired_field_configuration_error(exc) and await self._probe_after_retired_error():
            return await v2()
        raise _maybe_wrap_retired_field_configuration(exc) from exc

    async def delete_field_configuration(self, fc_id: str) -> None:
        if not await self._field_config_api_is_v2():
            try:
                await self.client.delete(f"{self.api_root}/fieldconfiguration/{fc_id}")
            except TransportError as exc:
                await self._retry_v2_after_retired(exc, lambda: self.delete_field_configuration(fc_id))
            return
        await self.delete_field_scheme(fc_id)

    async def update_field_configuration(
        self,
        fc_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Rename/re-describe a classic field configuration or a field scheme."""
        if not await self._field_config_api_is_v2():
            body: dict[str, Any] = {}
            if name is not None:
                body["name"] = name
            if description is not None:
                body["description"] = description
            if not body:
                return
            try:
                await self.client.put_json(f"{self.api_root}/fieldconfiguration/{fc_id}", json=body)
            except TransportError as exc:
                await self._retry_v2_after_retired(
                    exc,
                    lambda: self.update_field_configuration(fc_id, name=name, description=description),
                )
            return
        await self.update_field_scheme(fc_id, name=name, description=description)

    async def set_field_configuration_item(
        self,
        fc_id: str,
        *,
        field_id: str,
        required: bool = False,
        hidden: bool = False,
        description: str = "",
    ) -> None:
        if not await self._field_config_api_is_v2():
            try:
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
            except TransportError as exc:
                await self._retry_v2_after_retired(
                    exc,
                    lambda: self.set_field_configuration_item(
                        fc_id,
                        field_id=field_id,
                        required=required,
                        hidden=hidden,
                        description=description,
                    ),
                )
            return
        await self._set_field_scheme_item(
            fc_id,
            field_id=field_id,
            required=required,
            hidden=hidden,
            description=description,
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
        if not await self._field_config_api_is_v2():
            body: dict[str, Any] = {"name": name, "description": description}
            try:
                result = await self.client.post_json(
                    f"{self.api_root}/fieldconfigurationscheme",
                    json=body,
                )
            except TransportError as exc:
                result = await self._retry_v2_after_retired(
                    exc,
                    lambda: self.create_field_configuration_scheme(name=name, description=description),
                )
                # The re-entry routes to create_field_scheme, which returns
                # the id as a plain string.
                return str(result)
            return _expect_id(result, f"POST {self.api_root}/fieldconfigurationscheme")
        return await self.create_field_scheme(name=name, description=description)

    async def delete_field_configuration_scheme(self, scheme_id: str) -> None:
        if not await self._field_config_api_is_v2():
            try:
                await self.client.delete(f"{self.api_root}/fieldconfigurationscheme/{scheme_id}")
            except TransportError as exc:
                await self._retry_v2_after_retired(exc, lambda: self.delete_field_configuration_scheme(scheme_id))
            return
        await self.delete_field_scheme(scheme_id)

    async def update_field_configuration_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        if not await self._field_config_api_is_v2():
            body: dict[str, Any] = {}
            if name is not None:
                body["name"] = name
            if description is not None:
                body["description"] = description
            if not body:
                return
            try:
                await self.client.put_json(
                    f"{self.api_root}/fieldconfigurationscheme/{scheme_id}",
                    json=body,
                )
            except TransportError as exc:
                await self._retry_v2_after_retired(
                    exc,
                    lambda: self.update_field_configuration_scheme(scheme_id, name=name, description=description),
                )
            return
        await self.update_field_scheme(scheme_id, name=name, description=description)

    async def set_field_configuration_scheme_mappings(
        self,
        scheme_id: str,
        *,
        mappings: list[dict[str, str]],
    ) -> None:
        """mappings: [{"issueTypeId": "...", "fieldConfigurationId": "..."}, ...].

        At least one entry must have issueTypeId == "default".
        """
        if not await self._field_config_api_is_v2():
            try:
                await self.client.put_json(
                    f"{self.api_root}/fieldconfigurationscheme/{scheme_id}/mapping",
                    json={"mappings": mappings},
                )
            except TransportError as exc:
                await self._retry_v2_after_retired(
                    exc,
                    lambda: self.set_field_configuration_scheme_mappings(scheme_id, mappings=mappings),
                )
            return
        await self._set_field_scheme_mappings(scheme_id, mappings=mappings)

    async def set_project_field_configuration_scheme(
        self,
        *,
        project_id: str,
        scheme_id: str,
    ) -> None:
        if not await self._field_config_api_is_v2():
            try:
                await self.client.put_json(
                    f"{self.api_root}/fieldconfigurationscheme/project",
                    json={"fieldConfigurationSchemeId": scheme_id, "projectId": project_id},
                )
            except TransportError as exc:
                await self._retry_v2_after_retired(
                    exc,
                    lambda: self.set_project_field_configuration_scheme(project_id=project_id, scheme_id=scheme_id),
                )
            return
        await self.associate_projects_with_field_scheme(
            scheme_id,
            project_ids=[project_id],
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
        if not project_ids:
            async for entry in common.paginate(
                self.client,
                self._field_schemes_path(),
            ):
                yield entry
            return
        # Chunked so large tenants stay within the ``projectId`` cap. (M10)
        for chunk in self._chunked_ids(project_ids, self.PROJECT_FILTER_BATCH_SIZE):
            async for entry in common.paginate(
                self.client,
                self._field_schemes_path(),
                extra_params={"projectId": [_as_api_int(pid) for pid in chunk]},
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

        Rows whose ``fieldId`` the field registry doesn't know are dropped:
        Jira seeds a brand-new v2 field scheme with associations to fields
        its own registry no longer has (deleted custom fields,
        ``dataclassification``). Copying those rows back onto a scheme — the
        FCS merge does exactly that — 400s the whole association PUT. The
        registry lookup is best-effort: when it fails, nothing is filtered.
        """
        extra_params = {"fieldId": field_ids} if field_ids else None
        known_field_ids = await self._all_field_ids()
        async for entry in common.paginate(
            self.client,
            self._field_schemes_path(scheme_id, "fields"),
            extra_params=extra_params,
        ):
            row_field_id = entry.get("fieldId")
            if not row_field_id:
                continue
            if known_field_ids and str(row_field_id) not in known_field_ids:
                continue
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
            await self._put_field_scheme_associations(batch)

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

        The 200 response body carries the per-field outcome (M14), so it is
        fetched and inspected: only "not associated" failures are tolerated
        (that is the repeat-apply no-op); any other per-field failure is
        reported as a warning listing the rejected field ids, because the
        DELETE has already partially applied and the caller cannot unwind.
        A field we asked to remove that is still associated afterwards
        would silently keep the desired state wrong.
        """
        if not field_ids:
            return
        body = {field_id: {"schemeIds": [_as_api_int(scheme_id)]} for field_id in field_ids}
        for batch in _field_scheme_batches(body, self.FIELD_SCHEME_BATCH_SIZE):
            result = await self.client.delete_json(self._field_schemes_path("fields"), json=batch)
            self._raise_field_removal_failures(result)

    @staticmethod
    def _raise_field_removal_failures(result: Any) -> None:
        """Warn on per-field failures that are not the tolerated
        ``"not associated"`` case, with the offending field ids."""
        if not isinstance(result, list):
            return
        rejected: list[str] = []
        for entry in result:
            if not isinstance(entry, dict):
                continue
            field_id = str(entry.get("fieldId", ""))
            messages = [str(m) for m in entry.get("errorMessages", [])]
            errors = entry.get("errors") or {}
            messages += [str(v) for v in errors.values()] if isinstance(errors, dict) else []
            if not messages:
                continue
            if all("not associated" in m.lower() for m in messages):
                continue
            rejected.append(f"{field_id or '?'}: {'; '.join(messages)}")
        if rejected:
            warnings.warn(
                "field-scheme removal partially rejected: " + "; ".join(rejected),
                stacklevel=3,
            )

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
        await self._put_field_scheme_parameters({field_id: [entry]})

    async def _put_field_scheme_parameters(self, batch: dict[str, Any]) -> None:
        """PUT /config/fieldschemes/fields/parameters with immutable-parameter tolerance.

        Jira rejects the whole request whenever any field in it cannot take
        the parameter (live v2 tenant, 2026-11: 64 system/custom fields, e.g.
        "Field created cannot have required changed"). Such a field already
        behaves as-required as declared, so with the full per-item body
        (``TransportError.body``) the denied fields are dropped and the
        remainder re-sent, instead of splitting every field into its own
        request. An unparseable body or any unrecognized per-item error is a
        real failure and re-raises.
        """
        pending = dict(batch)
        # Each retry drops at least one field, so len(batch)+1 is enough to
        # converge; the bound keeps a pathological server from looping.
        for _ in range(len(batch) + 1):
            try:
                await self.client.put_json(self._field_schemes_path("fields", "parameters"), json=pending)
                return
            except TransportError as exc:
                if exc.status_code != 400:
                    raise
                body = _transport_body(exc)
                failed = _failed_field_rows(body)
                if failed is None:
                    if _parameter_denials_are_tolerable(body):
                        return
                    raise
                denied = {
                    field_id
                    for field_id, error in failed.items()
                    if any(fragment in error.lower() for fragment in _TOLERABLE_PARAMETER_ERROR_FRAGMENTS)
                }
                if failed.keys() - denied:
                    raise  # an unrecognized per-item failure must propagate
                drop = denied & pending.keys()
                if not drop:
                    raise  # denials name fields we never sent: cannot converge
                for field_id in drop:
                    del pending[field_id]
                if not pending:
                    return

    async def _put_field_scheme_associations(self, batch: dict[str, Any]) -> None:
        """PUT /config/fieldschemes/fields with per-field "doesn't exist" tolerance.

        Jira seeds a brand-new v2 field scheme with associations to fields
        its own field registry no longer knows (deleted custom fields,
        ``dataclassification``; live v2 tenant, 2026-11). Re-associating such
        ids 400s the whole request with per-item "Field <id> doesn't exist"
        rows. Those ids are dropped and the remainder re-sent; any other
        per-item failure, or an unclassifiable body, propagates.
        """
        pending = dict(batch)
        for _ in range(len(batch) + 1):
            try:
                await self.client.put_json(self._field_schemes_path("fields"), json=pending)
                return
            except TransportError as exc:
                if exc.status_code != 400:
                    raise
                failed = _failed_field_rows(_transport_body(exc))
                if failed is None:
                    raise
                drop = {
                    field_id for field_id, error in failed.items() if "doesn't exist" in error.lower()
                } & pending.keys()
                if not drop:
                    raise
                for field_id in drop:
                    del pending[field_id]
                if not pending:
                    return

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
        for chunk in self._chunked_ids(project_ids, self.PROJECT_FILTER_BATCH_SIZE):
            async for entry in common.paginate(
                self.client,
                self._field_schemes_path("projects"),
                extra_params={"projectId": [_as_api_int(pid) for pid in chunk]},
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
            await self._put_field_scheme_associations(batch)

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
            await self._put_field_scheme_parameters(batch)

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
        # A fresh work type won't be in the cached id list; without this,
        # "visible everywhere except X" restrictions silently exclude it. (M11)
        self._work_type_ids_cache = None
        return _expect_id(result, f"POST {self.api_root}/issuetype")

    async def delete_issuetype(self, issuetype_id: str) -> None:
        await self.client.delete(f"{self.api_root}/issuetype/{issuetype_id}")
        # The cached list still holds the deleted id, which would silently
        # appear in "all except" restrictions. Invalidate, mirroring the
        # field-id cache reset in create_custom_field/delete_custom_field. (M11)
        self._work_type_ids_cache = None

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
        return await self._resolve_user_id(lead)

    async def resolve_user(self, value: str) -> str:
        """Resolve an email to the platform user id for data-plane fields
        (assignee, reporter, UserField, MultiUserField).

        Same search and selection rules as the lead resolver and the same
        per-engine cache — one lookup per email no matter how many issues
        or fields name the same person. A value without an ``@`` is assumed
        already-resolved and passed through unchanged.
        """
        return await self._resolve_user_id(value)

    async def _resolve_user_id(self, value: str) -> str:
        """Shared user-search core for lead and field resolution."""
        if "@" not in value:
            return value
        cached = self._user_cache.get(value)
        if cached is not None:
            return cached
        try:
            users = await self.client.get_json(
                f"{self.api_root}/user/search",
                params={self.user_search_param: value},
            )
        except PermissionError as exc:
            raise ConfigurationError(
                f"Cannot resolve user {value!r}: the API token lacks the "
                f"'Browse users and groups' permission required for user search. "
                f"Grant it, or set the field to a resolved "
                f"{self.user_search_id_field} directly."
            ) from exc
        resolved = self._pick_user_id(value, users)
        self._user_cache[value] = resolved
        return resolved

    def _pick_user_id(self, value: str, users: Any) -> str:
        """Select the user id for ``value`` from a user-search result list.

        Prefers an exact (case-insensitive) ``emailAddress`` match; falls back
        to the sole result when email is hidden (GDPR strips it on Cloud).
        Raises ConfigurationError when nothing usable is found.
        """
        if not isinstance(users, list) or not users:
            raise ConfigurationError(f"No Jira user found for email {value!r}.")
        matches = [u for u in users if str(u.get("emailAddress", "")).lower() == value.lower()]
        if matches:
            chosen = matches[0]
        elif len(users) == 1:
            chosen = users[0]
        else:
            raise ConfigurationError(
                f"Email {value!r} matched {len(users)} users but none by exact "
                f"email; cannot disambiguate. Set the field to a resolved "
                f"{self.user_search_id_field} directly."
            )
        user_id = chosen.get(self.user_search_id_field)
        if not user_id:
            raise ConfigurationError(f"Jira user for {value!r} has no {self.user_search_id_field}.")
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

    async def add_project_role_actor(
        self,
        project_id: str,
        *,
        role_name: str,
        account_id: str,
    ) -> None:
        """Add one user (by accountId) to a project role, resolved by name.

        ``POST /project`` grants the lead no project role; tenants whose
        default permission scheme grants ``BROWSE_PROJECTS`` only through
        roles leave the lead unable to browse the project they own. Jira's
        own UI flow adds the creator to a role; the REST create does not, so
        ``__lead_role__`` reproduces that step (live-tenant observed,
        2026-11: lead had ``CREATE_ISSUES``/``EDIT_ISSUES`` but
        ``BROWSE_PROJECTS: false``).

        Re-adding an actor that is already in the role 400s ("already in");
        that is the idempotent re-run case and is treated as done.
        """
        role_id = await self.resolve_project_role_id(project_id, role_name)
        try:
            await self.client.post_json(
                f"{self.api_root}/project/{project_id}/role/{role_id}",
                json={"user": [account_id]},
            )
        except TransportError as exc:
            if exc.status_code == 400 and "already" in _transport_body(exc).lower():
                return
            raise

    async def resolve_project_role_id(self, project_id: str, role_name: str) -> str:
        """Resolve a project role by name to its id within a project."""
        roles = await self.client.get_json(f"{self.api_root}/project/{project_id}/role")
        if not isinstance(roles, dict):
            raise ConfigurationError(
                f"GET /project/{project_id}/role did not return a role-name → URL mapping (got {type(roles).__name__})."
            )
        for name, url in roles.items():
            if str(name).lower() == role_name.lower():
                return str(url).rstrip("/").rsplit("/", 1)[-1]
        raise ConfigurationError(
            f"Project {project_id} has no project role named {role_name!r}. Existing roles: {sorted(map(str, roles))}."
        )

    async def add_project_role_actors(
        self,
        project_id: str,
        *,
        role_id: str,
        users: list[str] | None = None,
        groups: list[str] | None = None,
    ) -> None:
        """Add users (accountIds) and/or groups (names) to a project role.

        Jira takes them as separate body keys on the same POST. Re-adding an
        actor that is already in the role 400s ("already in"); that is the
        idempotent re-run case and is treated as done.
        """
        body: dict[str, list[str]] = {}
        if users:
            body["user"] = list(users)
        if groups:
            body["group"] = list(groups)
        if not body:
            return
        try:
            await self.client.post_json(
                f"{self.api_root}/project/{project_id}/role/{role_id}",
                json=body,
            )
        except TransportError as exc:
            if exc.status_code == 400 and "already" in _transport_body(exc).lower():
                return
            raise

    async def remove_project_role_actor(
        self,
        project_id: str,
        *,
        role_id: str,
        user: str | None = None,
        group: str | None = None,
    ) -> None:
        """Remove one user (accountId) or group (name) from a project role.

        Jira takes the actor as a query parameter; one actor per request.
        A 404 (role gone, actor gone) is treated as already-removed so
        re-runs stay idempotent.
        """
        params: dict[str, str] = {}
        if user:
            params["user"] = user
        elif group:
            params["group"] = group
        else:
            return
        try:
            await self.client.delete(
                f"{self.api_root}/project/{project_id}/role/{role_id}",
                params=params,
            )
        except NotFoundError:
            return

    async def resolve_permission_holder(self, holder_type: str, holder_parameter: str) -> str:
        """Resolve a declared grant holder value to Jira's ``holder.parameter``
        form: user emails via user search; project role names via the global
        role listing. Groups and parameterless holders pass through."""
        if holder_type.lower() == "user":
            return await self.resolve_user(holder_parameter) if holder_parameter else ""
        if holder_type.lower() == "projectrole":
            if not holder_parameter:
                raise ConfigurationError("projectRole grant holder requires a role name")
            return await self.get_global_role_id(holder_parameter)
        return holder_parameter

    async def get_project_roles(self, project_id: str) -> dict[str, str]:
        """Role name → role id for one project (the /role URL map, resolved)."""
        roles = await self.client.get_json(f"{self.api_root}/project/{project_id}/role")
        if not isinstance(roles, dict):
            raise ReflectionError(
                f"GET {self.api_root}/project/{project_id}/role did not "
                f"return a role-name → URL mapping (got {type(roles).__name__})."
            )
        return {str(name): str(url).rstrip("/").rsplit("/", 1)[-1] for name, url in roles.items()}

    async def get_global_role_id(self, name: str) -> str:
        """Resolve a project role name to its id via the global ``GET /role``
        listing (cached). Role ids are tenant-global, so a permission-scheme
        grant can resolve its holder before any project exists."""
        if self._global_roles_cache is None:
            roles: dict[str, str] = {}
            async for entry in common.paginate(self.client, f"{self.api_root}/role"):
                role_name, role_id = common.parse_global_role(entry)
                if role_name and role_id:
                    roles[role_name] = role_id
            self._global_roles_cache = roles
        lowered = name.lower()
        for role_name, role_id in self._global_roles_cache.items():
            if role_name.lower() == lowered:
                return role_id
        raise ConfigurationError(
            f"No project role named {name!r} exists in Jira. Known roles: {sorted(self._global_roles_cache)}."
        )

    async def get_project_lead_account_id(self, project_id: str) -> str | None:
        """The project lead's accountId (``GET /project/{id}``), or None when
        the payload omits it. Used by the lockout guard."""
        payload = await self.client.get_json(f"{self.api_root}/project/{project_id}")
        lead = (payload or {}).get("lead") or {} if isinstance(payload, dict) else {}
        return str(lead.get("accountId", "")) or None

    async def get_project_role_actors(self, project_id: str, role_id: str) -> tuple[RoleActorSnapshot, ...]:
        """Read one project role's actors (used by the lockout guard)."""
        payload = await self.client.get_json(f"{self.api_root}/project/{project_id}/role/{role_id}")
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/project/{project_id}/role/{role_id} returned "
                f"{type(payload).__name__} instead of a role payload"
            )
        return common.parse_role_actors(payload.get("actors") or [])

    async def project_role_memberships(self, project_id: str, account_id: str) -> set[str]:
        """Names of the project roles ``account_id`` is an actor of.

        Used by the lockout guard (plan time and op time). Cached per
        (project, account) for the life of the dialect instance.
        """
        cache_key = (project_id, account_id)
        cached = self._role_membership_cache.get(cache_key)
        if cached is not None:
            return cached
        memberships: set[str] = set()
        for role_name, role_id in (await self.get_project_roles(project_id)).items():
            actors = await self.get_project_role_actors(project_id, role_id)
            if any(a.type == "user" and a.parameter == account_id for a in actors):
                memberships.add(role_name)
        self._role_membership_cache[cache_key] = memberships
        return memberships

    async def my_account_id(self) -> str:
        """The accountId stint authenticates as (``GET /myself``). Cached."""
        if self._my_account_id_cache is None:
            payload = await self.client.get_json(f"{self.api_root}/myself")
            account_id = (payload or {}).get("accountId") if isinstance(payload, dict) else None
            if not account_id:
                raise ReflectionError("GET /myself returned no accountId")
            self._my_account_id_cache = str(account_id)
        return self._my_account_id_cache

    async def user_groups(self, account_id: str) -> frozenset[str]:
        """Group names the user belongs to (``GET /user?expand=groups``). Cached."""
        cached = self._user_groups_cache.get(account_id)
        if cached is not None:
            return cached
        payload = await self.client.get_json(
            f"{self.api_root}/user",
            params={"accountId": account_id, "expand": "groups"},
        )
        groups = (payload or {}).get("groups") or {}
        names = frozenset(
            str(item.get("name", ""))
            for item in (groups.get("items") or [])
            if isinstance(item, dict) and item.get("name")
        )
        self._user_groups_cache[account_id] = names
        return names

    async def my_project_permissions(self, project_id: str, permissions: list[str]) -> dict[str, bool]:
        """``GET /mypermissions?permissions=...&projectId=...`` as
        permission-key → havePermission. Used by the lockout guard."""
        payload = await self.client.get_json(
            f"{self.api_root}/mypermissions",
            params={"permissions": ",".join(permissions), "projectId": project_id},
        )
        out: dict[str, bool] = {}
        entries = (payload or {}).get("permissions") or {}
        for key, entry in entries.items():
            if isinstance(entry, dict):
                out[str(key)] = bool(entry.get("havePermission", False))
        return out

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

    # ── Workflow transitions (data plane) ────────────────────────────
    async def get_transitions(self, key: str) -> list[dict[str, Any]]:
        """GET /issue/{key}/transitions. Returns the raw transition list.

        Each entry carries ``id``, ``name`` (the transition name, e.g. "Done")
        and ``to`` (the target status: ``{"name": ..., "statusCategory": {...}}``).
        """
        payload = await self.client.get_json(f"{self.api_root}/issue/{key}/transitions")
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/issue/{key}/transitions returned "
                f"{type(payload).__name__} instead of a transitions envelope"
            )
        transitions = payload.get("transitions", [])
        return [t for t in transitions if isinstance(t, dict)]

    async def perform_transition(self, key: str, body: dict[str, Any]) -> None:
        """POST /issue/{key}/transitions. Body is
        ``{"transition": {"id": ...}, "fields": {...}}``."""
        await self.client.post_json(f"{self.api_root}/issue/{key}/transitions", json=body)

    # ── Comments (data plane) ────────────────────────────────────────
    def get_comments(self, key: str) -> AsyncIterator[dict[str, Any]]:
        """Yield comments from ``GET /issue/{key}/comment``, paginated.

        The endpoint answers a ``{"comments": [...], "startAt": ...,
        "total": ...}`` envelope, hence ``envelope_key="comments"``.
        """
        return common.paginate(
            self.client,
            f"{self.api_root}/issue/{key}/comment",
            envelope_key="comments",
        )

    async def add_comment(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /issue/{key}/comment with ``{"body": <ADF>}``.

        Returns the created comment payload (id, author, created, body).
        """
        return await self.client.post_json(
            f"{self.api_root}/issue/{key}/comment",
            json={"body": body},
        )

    async def delete_comment(self, key: str, comment_id: str) -> None:
        await self.client.delete(f"{self.api_root}/issue/{key}/comment/{comment_id}")

    # ── Changelog (data plane) ───────────────────────────────────────
    def get_changelog(self, key: str) -> AsyncIterator[dict[str, Any]]:
        """Yield history entries from ``GET /issue/{key}/changelog``, paginated.

        Each entry carries ``author``, ``created`` and ``items`` (the
        per-field changes).
        """
        return common.paginate(
            self.client,
            f"{self.api_root}/issue/{key}/changelog",
            envelope_key="values",
        )

    # ── Attachments (data plane) ─────────────────────────────────────
    async def attach(self, key: str, *, data: bytes, filename: str) -> dict[str, Any]:
        """POST /issue/{key}/attachments, multipart.

        Jira requires the ``X-Atlassian-Token: no-check`` header on this
        endpoint (a CSRF guard; absent or wrong token answers 403). Some
        tenants answer with the created attachment object, some with a
        single-element array — the caller normalizes.
        """
        return await self.client.post_files(
            f"{self.api_root}/issue/{key}/attachments",
            files={"file": (filename, data)},
            headers={"X-Atlassian-Token": self.ATTACHMENT_TOKEN},
        )

    async def get_attachments(self, key: str) -> list[dict[str, Any]]:
        """One GET /issue/{key} with ``fields=attachment``; no expand needed."""
        payload = await self.client.get_json(
            f"{self.api_root}/issue/{key}",
            params={"fields": "attachment"},
        )
        fields = payload.get("fields") if isinstance(payload, dict) else None
        raw = (fields or {}).get("attachment") or []
        if not isinstance(raw, list):
            raise ReflectionError(
                f"GET {self.api_root}/issue/{key} fields.attachment returned {type(raw).__name__} instead of a list"
            )
        return [a for a in raw if isinstance(a, dict)]

    async def get_createmeta(self, project_key: str) -> dict[str, Any]:
        """GET /issue/createmeta for one project (read-only).

        Used by the runtime schema check to answer "settable on create":
        the response embeds, per issuetype, the fields on its create screen.
        ``expand=projects.issuetypes.fields`` is the expand Jira requires to
        actually include the fields: a bare ``expand=fields`` answers the
        issue types WITHOUT them, so every declared field reads as "not on
        the create screen" while inserts setting those fields succeed
        (live-tenant confirmed, 2026-11).
        """
        return await self.client.get_json(
            f"{self.api_root}/issue/createmeta",
            params={"projectKeys": project_key, "expand": "projects.issuetypes.fields"},
        )

    async def download_attachment(self, attachment_id: str) -> bytes:
        """GET /attachment/content/{id} as raw bytes.

        The endpoint may 302 to a pre-authenticated CDN URL, so redirects
        are followed for this call only.
        """
        return await self.client.get_bytes(
            f"{self.api_root}/attachment/content/{attachment_id}",
            follow_redirects=True,
        )

    async def delete_attachment(self, attachment_id: str) -> None:
        await self.client.delete(f"{self.api_root}/attachment/{attachment_id}")

    # ── Issue links (data plane) ─────────────────────────────────────
    async def get_issuelinks(self, key: str) -> list[dict[str, Any]]:
        """One GET /issue/{key} with ``fields=issuelinks``; no extra request
        per link."""
        payload = await self.client.get_json(
            f"{self.api_root}/issue/{key}",
            params={"fields": "issuelinks"},
        )
        fields = payload.get("fields") if isinstance(payload, dict) else None
        raw = (fields or {}).get("issuelinks") or []
        if not isinstance(raw, list):
            raise ReflectionError(
                f"GET {self.api_root}/issue/{key} fields.issuelinks returned {type(raw).__name__} instead of a list"
            )
        return [entry for entry in raw if isinstance(entry, dict)]

    async def create_issue_link(self, *, link_type: str, outward_key: str, inward_key: str) -> None:
        """POST /issueLink. ``outward_key`` is the subject (A in "A blocks
        B"), ``inward_key`` the object (B).

        Jira's create semantics are counter-intuitive: the issue listed under
        ``inwardIssue`` is the one that ends up carrying the link type's
        *outward* description ("blocks"). The subject A must therefore go
        under ``inwardIssue`` and the object B under ``outwardIssue`` —
        sending them the other way round creates the link reversed
        ("B blocks A") with the read side still reporting consistently
        (live-tenant confirmed, 2026-11).
        """
        await self.client.post_json(
            f"{self.api_root}/issueLink",
            json={
                "type": {"name": link_type},
                "inwardIssue": {"key": outward_key},
                "outwardIssue": {"key": inward_key},
            },
        )

    async def delete_issue_link(self, link_id: str) -> None:
        await self.client.delete(f"{self.api_root}/issueLink/{link_id}")

    # ── Issue security schemes ────────────────────────────────────────────

    async def _reflect_issue_security_schemes(self) -> dict[str, SecuritySchemeSnapshot]:
        """Reflect all issue security schemes from the Jira instance.

        Returns a dict mapping scheme ID -> SecuritySchemeSnapshot.

        ``GET /issuesecurityschemes`` takes no query parameters and answers a
        bare ``{"issueSecuritySchemes": [...]}`` envelope — no ``startAt``,
        no ``isLast`` — so it must NOT go through ``common.paginate``: the
        page-2 request is silently ignored and returns the same list, a
        non-empty page with no ``isLast``, and reflect hangs forever re-
        reading page 1 (the reported ``revision --autogenerate`` hang, whose
        only output was the synthetic-screens warning one step earlier).
        The endpoint never includes ``levels`` (H8), so each scheme is still
        fetched individually with the single-scheme GET which embeds them;
        without this every reflect produced ``levels=[]`` and any declared
        levels diffed as phantom drift forever.
        """
        payload = await self.client.get_json(f"{self.api_root}/issuesecurityschemes")
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/issuesecurityschemes returned "
                f"{type(payload).__name__} instead of an issueSecuritySchemes envelope"
            )
        result: dict[str, SecuritySchemeSnapshot] = {}
        for entry in payload.get("issueSecuritySchemes") or []:
            if not isinstance(entry, dict):
                raise ReflectionError(
                    f"GET {self.api_root}/issuesecurityschemes included a non-object scheme entry: {str(entry)[:200]!r}"
                )
            scheme = common.parse_issue_security_scheme(entry)
            scheme = dataclasses.replace(
                scheme,
                levels=list((await self._reflect_issue_security_levels(scheme.id)).values()),
            )
            result[scheme.id] = scheme
        return result

    async def get_issue_security_levels(self, scheme_id: str) -> list[SecurityLevelSnapshot]:
        """Public read for op bookkeeping: one GET of the scheme, returning
        levels with their Jira ids and default flags (no member fetch — the
        security-scheme write endpoints return no level ids, so ops capture
        them here right after create/add; members are tracked in state)."""
        payload = await self.client.get_json(f"{self.api_root}/issuesecurityschemes/{scheme_id}")
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/issuesecurityschemes/{scheme_id} returned "
                f"{type(payload).__name__} instead of a scheme payload"
            )
        return [
            SecurityLevelSnapshot(
                id=str(level.get("id", "")),
                name=str(level.get("name", "")),
                description=str(level.get("description", "")),
                is_default=bool(level.get("isDefault", False)),
            )
            for level in payload.get("levels") or []
            if isinstance(level, dict)
        ]

    async def _reflect_issue_security_levels(self, scheme_id: str) -> dict[str, SecurityLevelSnapshot]:
        """Reflect all security levels for a given issue security scheme.

        Returns a dict mapping level ID -> SecurityLevelSnapshot, in the
        order the scheme lists them.

        The list endpoint omits ``levels`` entirely, so this uses the
        single-scheme GET (``GET /issuesecurityschemes/{id}``), whose
        payload embeds them; a level without inline ``members`` falls back
        to the level-member endpoint (H8).
        """
        result: dict[str, SecurityLevelSnapshot] = {}
        payload = await self.client.get_json(f"{self.api_root}/issuesecurityschemes/{scheme_id}")
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/issuesecurityschemes/{scheme_id} returned "
                f"{type(payload).__name__} instead of a scheme payload"
            )
        for level_entry in payload.get("levels") or []:
            if not isinstance(level_entry, dict):
                raise ReflectionError(
                    f"GET {self.api_root}/issuesecurityschemes/{scheme_id} included "
                    f"a non-object level entry: {str(level_entry)[:200]!r}"
                )
            level_id = str(level_entry.get("id", ""))
            raw_members = level_entry.get("members")
            # Fallback not only when the key is absent but also when it is an
            # empty list: Jira strips inline ``members`` on GETs it computes
            # elsewhere, and treating that as "the level has no members"
            # hides them from the diff, which then re-emits member adds that
            # Jira rejects as duplicates. A genuinely memberless level
            # costs one extra GET that returns an empty page.
            if not raw_members:
                members_by_id = await self._reflect_issue_security_level_members(scheme_id, level_id)
                members = list(members_by_id.values())
            else:
                members = []
                for m in raw_members:
                    holder = m.get("holder", {}) if isinstance(m, dict) else {}
                    members.append(
                        SecurityLevelMemberSnapshot(
                            id=str(m.get("id", "")),
                            type=str(holder.get("type", "unknown")),
                            name=str(holder.get("name", "")),
                            # holder.parameter is the group name / accountId that
                            # member-add and member-diff compare against; fall
                            # back to name for payloads that only carry it.
                            parameter=str(holder.get("parameter", "") or holder.get("name", "")),
                        )
                    )
            result[level_id] = SecurityLevelSnapshot(
                id=level_id,
                name=str(level_entry.get("name", "")),
                description=str(level_entry.get("description", "")),
                is_default=level_entry.get("isDefault", False),
                members=tuple(members),
            )
        return result

    async def _reflect_issue_security_level_members(
        self,
        scheme_id: str,
        level_id: str,
    ) -> dict[str, SecurityLevelMemberSnapshot]:
        """Reflect all members for a given security level.

        Returns a dict mapping member ID -> SecurityLevelMemberSnapshot, keyed
        with a synthetic placeholder for entries that carry no ``id``.

        The member endpoint is a paginated ``PageBeanSecurityLevelMember``, so
        it must go through ``paginate`` — a single page silently truncates and
        the diff invents phantom member adds for everything past the cut.
        """
        result: dict[str, SecurityLevelMemberSnapshot] = {}
        page = 0
        async for member in common.paginate(
            self.client,
            f"{self.api_root}/issuesecurityschemes/level/member",
            extra_params={"schemeId": scheme_id, "levelId": level_id},
        ):
            if not isinstance(member, dict):
                raise ReflectionError(
                    f"security level {level_id!r} of scheme {scheme_id!r} included "
                    f"a non-object member entry: {str(member)[:200]!r}"
                )
            holder = member.get("holder", {})
            # Members without an "id" (observed on some holder types, e.g.
            # parameterless ones) must not collapse onto one " " key — last
            # one wins would truncate the member list.
            member_id = str(member.get("id", "")) or f"idx-{page}"
            page += 1
            result[member_id] = SecurityLevelMemberSnapshot(
                id=str(member.get("id", "")),
                type=str(holder.get("type", "unknown")),
                name=str(holder.get("name", "")),
                # holder.parameter is the group name / accountId that member
                # add/remove and diff compare against (H8 follow-up).
                parameter=str(holder.get("parameter", "") or holder.get("name", "")),
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
        # The only create in this dialect that parsed `result["id"]` raw: an
        # empty 2xx body would crash with AttributeError and leave the scheme
        # created but untracked. Use the shared validator (M16).
        return _expect_id(result, f"POST {self.api_root}/issuesecurityschemes")

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
        if not body:
            # An empty PUT body is rejected by Jira with a 400; a no-op
            # apply must not put a request on the wire at all (like every
            # sibling updater).
            return
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
        try:
            await self.client.put_json(
                f"{self.api_root}/issuesecurityschemes/{scheme_id}/level/{level_id}/member",
                json=body,
            )
        except TransportError as exc:
            # A member already in the level (added out-of-band, or by a run
            # that crashed between the PUT and the state persist) makes re-runs
            # 400 with this exact body. That member is present, so swallowing
            # the call turns a mixed-scheme upgrade into a no-op instead of an
            # abort that would repeat forever — state can never record a call
            # that raises, and the diff would re-emit the add each revision.
            if _is_duplicate_member_400(exc):
                return
            raise

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

    async def add_issue_security_levels(
        self,
        scheme_id: str,
        *,
        levels: list[dict[str, Any]],
    ) -> None:
        """Add levels to an existing scheme.

        ``levels`` entries: ``{"name": ..., "description": ..., "isDefault": bool}``.
        The response is 204 with no body — no level ids; capture them via
        ``get_issue_security_levels`` right after (same call as after create).

        **Permissions required:** Administer Jira global permission.

        **OAuth scopes:** `manage:jira-configuration`
        """
        await self.client.put_json(
            f"{self.api_root}/issuesecurityschemes/{scheme_id}/level",
            json={"levels": levels},
        )

    async def set_default_issue_security_levels(self, defaults: dict[str, str | None]) -> None:
        """Set (or clear) each scheme's default level: ``{scheme_id: level_id}``.

        A ``None`` value sends ``"defaultLevelId": null``, which the API
        documents as "reset the default level": issues then get no security
        level and stay visible to anyone who can browse the project. An empty
        mapping puts no request on the wire (an empty body 400s, like every
        sibling updater).

        **Permissions required:** Administer Jira global permission.

        **OAuth scopes:** `manage:jira-configuration`
        """
        if not defaults:
            return
        await self.client.put_json(
            f"{self.api_root}/issuesecurityschemes/level/default",
            json={
                "defaultValues": [
                    {"issueSecuritySchemeId": sid, "defaultLevelId": lid} for sid, lid in defaults.items()
                ]
            },
        )

    async def resolve_security_level_member(self, *, member_type: str, member_value: str) -> str:
        """Normalize a declared member to Jira's ``holder.parameter`` form.

        Groups pass through as group names. Users pass through unless the
        declared value carries ``@`` (an email), which resolves through the
        user-search API to an accountId — reusing ``resolve_lead``'s
        search + disambiguation, which needs "Browse users and groups".
        """
        if member_type == "user":
            return await self.resolve_lead(member_value)
        return member_value

    # ── Permission schemes ───────────────────────────────────────────

    async def create_permission_scheme(
        self,
        *,
        name: str,
        description: str = "",
        permissions: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a permission scheme, optionally with inline grants.

        ``permissions`` entries: ``{"permission": <key>, "holder":
        {"type": <type>, "parameter": <param?>}}``. User holders must carry
        resolved accountIds; projectRole holders must carry resolved role
        ids (resolve both before calling).

        **Permissions required:** *Administer Jira* global permission.
        """
        body: dict[str, Any] = {"name": name, "description": description}
        if permissions:
            body["permissions"] = permissions
        result = await self.client.post_json(f"{self.api_root}/permissionscheme", json=body)
        return _expect_id(result, f"POST {self.api_root}/permissionscheme")

    async def update_permission_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Update a permission scheme's name/description only.

        The PUT endpoint *replaces all grants* when a permissions list is
        present, so this method never sends one — grant changes go through
        ``add_permission_grant`` / ``remove_permission_grant``.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        await self.client.put_json(f"{self.api_root}/permissionscheme/{scheme_id}", json=body)

    async def delete_permission_scheme(self, scheme_id: str) -> None:
        """Delete a permission scheme. Jira refuses while a project uses it;
        the emitted delete order unbinds projects first."""
        await self.client.delete(f"{self.api_root}/permissionscheme/{scheme_id}")

    async def get_permission_scheme(self, scheme_id: str) -> PermissionSchemeSnapshot:
        """One permission scheme with its grants (``expand=permissions``).

        Read for op bookkeeping: grant ids are needed for removals and are
        not returned by the grant POST.
        """
        payload = await self.client.get_json(
            f"{self.api_root}/permissionscheme/{scheme_id}",
            params={"expand": "permissions"},
        )
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/permissionscheme/{scheme_id} returned "
                f"{type(payload).__name__} instead of a scheme payload"
            )
        return common.parse_permission_scheme(payload)

    async def find_permission_scheme_id_by_name(self, name: str) -> str | None:
        """Match an existing scheme by name (``copy_from`` resolution)."""
        payload = await self.client.get_json(f"{self.api_root}/permissionscheme")
        if not isinstance(payload, dict):
            return None
        for entry in payload.get("permissionSchemes") or []:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
                return str(entry["id"])
        return None

    async def add_permission_grant(
        self,
        scheme_id: str,
        *,
        permission: str,
        holder_type: str,
        holder_parameter: str = "",
    ) -> str:
        """Add one grant to a permission scheme; returns the new grant id.

        Re-adding an identical grant 400s ("already exists"); that is the
        idempotent re-run case and is treated as done (the existing grant id
        is then resolved by the caller when needed).
        """
        holder: dict[str, str] = {"type": holder_type}
        if holder_parameter:
            holder["parameter"] = holder_parameter
        try:
            result = await self.client.post_json(
                f"{self.api_root}/permissionscheme/{scheme_id}/permission",
                json={"permission": permission, "holder": holder},
            )
        except TransportError as exc:
            if exc.status_code == 400 and "already" in _transport_body(exc).lower():
                return ""
            raise
        return _expect_id(result, f"POST {self.api_root}/permissionscheme/{scheme_id}/permission")

    async def remove_permission_grant(self, scheme_id: str, grant_id: str) -> None:
        """Delete one grant by id. A 404 is treated as already-removed."""
        try:
            await self.client.delete(f"{self.api_root}/permissionscheme/{scheme_id}/permission/{grant_id}")
        except NotFoundError:
            return

    async def assign_permission_scheme(self, *, project_id: str, scheme_id: str) -> None:
        """Bind a permission scheme to a project. Synchronous (unlike the
        issue-security-scheme association, there is no task to poll)."""
        await self.client.put_json(
            f"{self.api_root}/project/{project_id}/permissionscheme",
            json={"id": scheme_id},
        )

    async def get_project_permission_scheme_id(self, project_id: str) -> str | None:
        """The id of the scheme bound to a project (always exists in Jira)."""
        payload = await self.client.get_json(f"{self.api_root}/project/{project_id}/permissionscheme")
        if isinstance(payload, dict) and payload.get("id") is not None:
            return str(payload["id"])
        return None

    # ── Workflows (new-model bulk APIs) ──────────────────────────────

    async def create_workflows(
        self,
        *,
        statuses: list[dict[str, Any]],
        workflows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk-create workflows + their statuses (``POST /workflows/create``).

        ``statuses`` entries: ``WorkflowStatusUpdate`` dicts (name,
        statusCategory, statusReference, optional id/description).
        ``workflows`` entries: ``WorkflowCreate`` dicts (name, statuses by
        statusReference, transitions). Synchronous 200; 409 means another
        workflow-config task is running — surfaced as TransportError.
        Returns the response body (workflows with real ids + versions,
        statuses with real numeric ids).
        """
        return await self.client.post_json(
            f"{self.api_root}/workflows/create",
            json={"scope": {"type": "GLOBAL"}, "statuses": statuses, "workflows": workflows},
        )

    async def update_workflows(
        self,
        *,
        statuses: list[dict[str, Any]],
        workflows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk-update workflows (``POST /workflows/update``).

        Each workflow entry must carry its ``id`` (UUID) and current
        ``version`` (optimistic locking). The payload is the full
        declarative state of each workflow. Returns the response body;
        its optional ``taskId`` must be polled to completion by the caller
        (a side migration may still be running after the synchronous 200).
        """
        return await self.client.post_json(
            f"{self.api_root}/workflows/update",
            json={"statuses": statuses, "workflows": workflows},
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """One task-progress bean (``GET /task/{taskId}``)."""
        payload = await self.client.get_json(f"{self.api_root}/task/{task_id}")
        return payload if isinstance(payload, dict) else {}

    async def read_workflow_by_name(self, name: str) -> WorkflowSnapshot | None:
        """The workflow matching a name, parsed into a snapshot
        (``GET /workflows/search?queryString=``).

        Used by adoption and by ops that need the live id/version/status
        set. Returns None when no workflow with that (case-insensitive)
        name exists. Note the matched statuses may not cover every status
        referenced by *other* workflows on the same page — parse resolves
        what it has; unreferenced ones are simply absent.
        """
        payload = await self.client.get_json(
            f"{self.api_root}/workflows/search",
            params={"queryString": name, "maxResults": 1},
        )
        if not isinstance(payload, dict):
            return None
        statuses_by_id = common.parse_workflow_status_map(payload.get("statuses") or [])
        for entry in payload.get("values") or []:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
                return common.parse_workflow(entry, statuses_by_id)
        return None

    async def resolve_status_ids(self, names: list[str]) -> dict[str, str | None]:
        """Global status name → numeric id (``GET /statuses/byNames``).

        Status names are NOT tenant-unique (different scopes can share a
        name and the API resolves "the first found"), so unresolved names
        are returned as None and callers must treat creating a duplicate
        as a Jira-side decision instead of guessing a lookup.
        """
        if not names:
            return {}
        params: list[tuple[str, str]] = [("name", n) for n in names]
        payload = await self.client.get_json(f"{self.api_root}/statuses/byNames", params=params)
        found: dict[str, str] = {}
        for entry in payload or []:
            if isinstance(entry, dict) and entry.get("name") and entry.get("id"):
                found.setdefault(str(entry["name"]), str(entry["id"]))
        return {name: found.get(name) for name in names}

    async def global_project_role_ids(self) -> dict[str, str]:
        """Global project-role name → id (``GET /rest/api/3/role``).

        The restrict-issue-transition rule's ``roleIds`` parameter carries
        role *ids* while declarations name roles."""
        try:
            out: dict[str, str] = {}
            async for entry in common.paginate(self.client, f"{self.api_root}/role"):
                name, role_id = common.parse_global_role(entry)
                if name and role_id:
                    out[name] = role_id
            return out
        except NotFoundError:
            return {}

    async def resolve_group_ids(self, names: list[str]) -> dict[str, str]:
        """Group name → groupId (``GET /group/bulk?groupName=``).

        The restrict-issue-transition rule's ``groupIds`` parameter carries
        group *ids* while declarations name groups. Names that match no
        group are omitted; the caller raises with the unmatched names."""
        out: dict[str, str] = {}
        for name in names:
            payload = await self.client.get_json(
                f"{self.api_root}/group/bulk",
                params={"groupName": name, "maxResults": 50},
            )
            for entry in (payload or {}).get("values") or []:
                if (
                    isinstance(entry, dict)
                    and str(entry.get("name", "")).lower() == name.lower()
                    and entry.get("groupId")
                ):
                    out[name] = str(entry["groupId"])
                    break
        return out

    # ── Workflow schemes ─────────────────────────────────────────────

    async def create_workflow_scheme(
        self,
        *,
        name: str,
        description: str = "",
        default_workflow: str = "",
        issue_type_mappings: dict[str, str] | None = None,
    ) -> str:
        """Create a workflow scheme (``POST /workflowscheme``) → its id.

        The classic shape: ``defaultWorkflow`` and ``issueTypeMappings``
        values are workflow *names*. Requires *Administer Jira*.
        """
        body: dict[str, Any] = {"name": name, "description": description}
        if default_workflow:
            body["defaultWorkflow"] = default_workflow
        if issue_type_mappings:
            body["issueTypeMappings"] = issue_type_mappings
        result = await self.client.post_json(f"{self.api_root}/workflowscheme", json=body)
        return _expect_id(result, f"POST {self.api_root}/workflowscheme")

    async def update_workflow_scheme(
        self,
        scheme_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        default_workflow: str | None = None,
        issue_type_mappings: dict[str, str] | None = None,
    ) -> None:
        """Update a workflow scheme (``PUT /workflowscheme/{id}``).

        Only the fields passed are sent. Note Jira's contract: an active
        scheme is edited through a draft when ``updateDraftIfNeeded`` is
        true — stint omits the flag so direct updates on inactive schemes
        apply immediately, and scheme *rebinds* on live projects go through
        ``switch_project_workflow_scheme`` instead.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if default_workflow is not None:
            body["defaultWorkflow"] = default_workflow
        if issue_type_mappings is not None:
            body["issueTypeMappings"] = issue_type_mappings
        if not body:
            return
        await self.client.put_json(f"{self.api_root}/workflowscheme/{scheme_id}", json=body)

    async def delete_workflow_scheme(self, scheme_id: str) -> None:
        """Delete a workflow scheme. Jira refuses while a project uses it;
        the emitted order unbinds projects first."""
        await self.client.delete(f"{self.api_root}/workflowscheme/{scheme_id}")

    async def find_workflow_scheme_id_by_name(self, name: str) -> str | None:
        """Match an existing workflow scheme by name (adoption fallback)."""
        async for entry in common.paginate(self.client, f"{self.api_root}/workflowscheme"):
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
                return str(entry["id"])
        return None

    async def get_workflow_scheme(self, scheme_id: str) -> WorkflowSchemeSnapshot:
        """One workflow scheme (``GET /workflowscheme/{id}``) — the classic
        shape (workflows referenced by name), used to evaluate the
        transition guard against the scheme a project currently binds."""
        payload = await self.client.get_json(f"{self.api_root}/workflowscheme/{scheme_id}")
        if not isinstance(payload, dict):
            raise ReflectionError(
                f"GET {self.api_root}/workflowscheme/{scheme_id} returned "
                f"{type(payload).__name__} instead of a scheme payload"
            )
        return common.parse_workflow_scheme(payload)

    async def delete_workflow(self, workflow_id: str) -> None:
        """Delete an inactive workflow (``DELETE /workflow/{entityId}``).

        Jira refuses for active/system/scheme-associated workflows; the
        emitted order deletes schemes (and unbinds projects) first."""
        await self.client.delete(f"{self.api_root}/workflow/{workflow_id}")

    async def associate_workflow_scheme(self, *, project_id: str, scheme_id: str | None) -> None:
        """Bind a workflow scheme to an *empty* project
        (``PUT /workflowscheme/project``). Jira documents this as working
        only when the project has no issues; rebinding a live project must
        go through ``switch_project_workflow_scheme``. ``scheme_id=None``
        assigns the default scheme (unbinding a custom one)."""
        await self.client.put_json(
            f"{self.api_root}/workflowscheme/project",
            json={"projectId": project_id, "workflowSchemeId": scheme_id},
        )

    async def switch_project_workflow_scheme(
        self,
        *,
        project_id: str,
        target_scheme_id: str,
        mappings_by_issue_type_override: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Rebind a live project's workflow scheme
        (``POST /workflowscheme/project/switch``).

        Mappings are required for statuses that would disappear under the
        target scheme (discovered via ``get_required_scheme_mappings``).
        Returns the task bean (the endpoint answers 303 See Other; the
        client follows the Location header and unwraps the task). The
        caller polls it to a terminal state.
        """
        body: dict[str, Any] = {"projectId": project_id, "targetSchemeId": target_scheme_id}
        if mappings_by_issue_type_override:
            body["mappingsByIssueTypeOverride"] = mappings_by_issue_type_override
        return await self.client.post_for_task(f"{self.api_root}/workflowscheme/project/switch", json=body)

    async def get_required_scheme_mappings(
        self,
        *,
        scheme_id: str,
        workflows_for_issue_types: list[dict[str, Any]],
        default_workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Which statuses require migration mappings for a scheme change
        (``POST /workflowscheme/update/mappings``).

        ``workflows_for_issue_types`` entries: ``{workflowId, issueTypeIds}``
        with new-model workflow UUIDs. Response carries the statuses
        requiring mapping per issue type and per workflow pair. This is a
        *fact read* — the old→new target mapping itself is never guessed.
        """
        body: dict[str, Any] = {"id": scheme_id, "workflowsForIssueTypes": workflows_for_issue_types}
        if default_workflow_id:
            body["defaultWorkflowId"] = default_workflow_id
        payload = await self.client.post_json(f"{self.api_root}/workflowscheme/update/mappings", json=body)
        return payload if isinstance(payload, dict) else {}


def _expect_id(result: Any, where: str) -> str:
    if not isinstance(result, dict) or "id" not in result:
        raise ReflectionError(f"{where} returned no id: {result!r}")
    return str(result["id"])


def _transport_body(exc: TransportError) -> str:
    """The full error body when available, else the exception message.

    ``TransportError.body`` carries the untruncated response text; older
    messages (or callers constructing TransportError by hand) only have the
    truncated text in the message string.
    """
    if isinstance(exc.body, str) and exc.body:
        return exc.body
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return ""


_RETIRED_FC_HINTS = ("Cannot create a new field configuration", "use Field Scheme instead")


# Per-item parameter errors the field-schemes API reports for system fields
# whose parameters Jira won't let a caller change. "Cannot have required
# changed" for `summary` means the field already behaves as required — a
# no-op, not a failure (live-tenant observed, 2026-11).
_TOLERABLE_PARAMETER_ERROR_FRAGMENTS = (
    "cannot have required changed",
    "cannot have description changed",
)


def _parameter_denials_are_tolerable(error_body: str) -> bool:
    """True when every per-item failure in a /fields/parameters 400 body is
    a known-immutable system-field parameter change (see the tuples above).
    Non-JSON bodies or any unrecognized error must NOT be swallowed."""
    body = error_body.strip()
    start = body.find("{")
    if start < 0:
        return False
    try:
        parsed = json.loads(body[start:])
    except (json.JSONDecodeError, TypeError):
        return False
    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list) or not results:
        return False
    for row in results:
        if not isinstance(row, dict) or row.get("success", True):
            continue
        error = str(row.get("error", "")).lower()
        if not any(fragment in error for fragment in _TOLERABLE_PARAMETER_ERROR_FRAGMENTS):
            return False
    return True


def _failed_field_rows(error_body: str) -> dict[str, str] | None:
    """fieldId → error text for every failed row of a per-item Jira response.

    The v2 field-scheme association and parameter endpoints report per-item
    outcomes as ``{"results": [{"fieldId", "success", "error"}, ...]}``. The
    HTTP status is a single 400 even though only some rows failed, so callers
    use the field ids to drop-and-retry. Returns None when the body is not
    that shape (callers must not swallow unclassifiable errors).
    """
    body = error_body.strip()
    start = body.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(body[start:])
    except (json.JSONDecodeError, TypeError):
        return None
    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list):
        return None
    failed: dict[str, str] = {}
    for row in results:
        if not isinstance(row, dict) or row.get("success", True):
            continue
        field_id = str(row.get("fieldId", ""))
        error = str(row.get("error", ""))
        if field_id:
            failed[field_id] = error
    return failed


def _is_retired_field_configuration_error(exc: TransportError) -> bool:
    """True for the specific 400 Jira returns for the retired classic API."""
    if exc.status_code != 400:
        return False
    return all(hint in _transport_body(exc) for hint in _RETIRED_FC_HINTS)


def _is_duplicate_member_400(exc: TransportError) -> bool:
    """True for the exact 400 Jira returns when re-adding a security-level
    member that is already in the level: ``{"errorMessages": ["Issue
    security scheme level member with the type user already exists."],
    "errors": {}}``.

    Conservative like ``_parameter_denials_are_tolerable``: only a JSON body
    whose *every* error message says "already exists" (and no per-field
    errors) counts. Anything else — permission, malformed id, unknown
    payload — must propagate."""
    if exc.status_code != 400:
        return False
    body = _transport_body(exc)
    start = body.find("{")
    if start < 0:
        return False
    try:
        parsed = json.loads(body[start:])
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    messages = parsed.get("errorMessages")
    if not isinstance(messages, list) or not messages:
        return False
    if any("already exists" not in str(message) for message in messages):
        return False
    errors = parsed.get("errors")
    return not errors


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
    body = _transport_body(exc)
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
