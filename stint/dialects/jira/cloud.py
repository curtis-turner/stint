"""Jira Cloud dialect.

Cloud paths live under `/rest/api/3`. APIToken (email + token, basic-style) is
the recommended auth. Custom field options live under a context, so option
lookup overrides the base implementation.

TMP handling (team-managed projects, ``style == "next-gen"``) is reflected
into ProjectSnapshot.style. Apply-side TMP partial support lands in M8.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from stint.dialects.jira import common
from stint.dialects.jira._base import JiraDialectBase
from stint.exceptions import ReflectionError


class JiraCloudDialect(JiraDialectBase):
    name = "jira_cloud"
    api_root = "/rest/api/3"
    expected_deployment_type = "Cloud"
    # Cloud's field-configuration items endpoint is /fieldconfiguration/{id}/fields
    # (per the Cloud Platform OpenAPI). DC keeps the inherited /items default.
    field_config_items_segment = "fields"

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

    async def _reflect_field_options(self, field_id: str) -> dict[str, str]:
        """Cloud option fetch goes through the field context.

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
        from stint.dialects.jira._base import _expect_id

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

    async def delete_custom_field_option(
        self,
        field_id: str,
        option_id: str,
    ) -> None:
        """Cloud option delete goes through the field context."""
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
