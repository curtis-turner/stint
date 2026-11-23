"""Data plane: email-to-user resolution for user-typed fields — issue #49."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import httpx
import pytest
import respx

from stint import AsyncSession, PartialCommitError, PATAuth, StateFile, create_engine
from stint.engine import Engine
from stint.fields import CustomField, MultiUserField, UserField
from stint.registry import registry
from stint.schema.issuetype import IssueType
from stint.state.file import (
    CustomFieldMapping,
    ProjectMapping,
    SimpleMapping,
)

BASE = "https://jira.example.com"
CLOUD_ROOT = f"{BASE}/rest/api/3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)
    yield
    registry.reset()
    sys.modules.pop("examples.company_managed.platform_schema", None)


def _cloud_engine() -> Engine:
    return create_engine(f"jira_cloud+{BASE}", auth=PATAuth("tok"))


def _state() -> StateFile:
    state = StateFile(env="dev", jira_url=BASE)
    state.custom_fields["approver"] = CustomFieldMapping(id="customfield_10050")
    state.custom_fields["watchers"] = CustomFieldMapping(id="customfield_10051")
    state.issuetypes["assignee_ticket"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


approver_field = CustomField(
    alias="approver",
    name="Approver",
    type=UserField,
)
watchers_field = CustomField(
    alias="watchers",
    name="Watchers",
    type=MultiUserField,
)


class AssigneeTicket(IssueType):
    __alias__ = "assignee_ticket"
    summary: str
    assignee: str | None = None
    approver: Annotated[str | None, approver_field] = None
    watchers: Annotated[list[str] | None, watchers_field] = None


def _user(account_id: str, email: str) -> dict:
    return {"accountId": account_id, "emailAddress": email, "displayName": email.split("@")[0]}


_SEARCH_RESULT = [_user("acc-aaa", "alice@example.com")]


def _mock_user_search():
    return respx.get(f"{CLOUD_ROOT}/user/search").mock(return_value=httpx.Response(200, json=_SEARCH_RESULT))


# ── inserts ──────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_insert_resolves_assignee_email_to_account_id():
    _mock_user_search()
    post = respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"key": "PLAT-1", "id": "1"}))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(AssigneeTicket(summary="boom", assignee="alice@example.com"), project="PLAT")
        await session.commit()
    body = json.loads(post.calls[0].request.read())
    assert body["fields"]["assignee"] == {"accountId": "acc-aaa"}


@respx.mock
@pytest.mark.asyncio
async def test_insert_resolves_user_field_email():
    _mock_user_search()
    post = respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"key": "PLAT-1", "id": "1"}))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(AssigneeTicket(summary="boom", approver="alice@example.com"), project="PLAT")
        await session.commit()
    body = json.loads(post.calls[0].request.read())
    assert body["fields"]["customfield_10050"] == {"accountId": "acc-aaa"}


@respx.mock
@pytest.mark.asyncio
async def test_insert_resolves_multiuser_items_individually():
    _mock_user_search()
    respx.get(f"{CLOUD_ROOT}/user/search").mock(
        side_effect=[
            httpx.Response(200, json=_SEARCH_RESULT),
            httpx.Response(200, json=[_user("acc-bbb", "bob@example.com")]),
        ]
    )
    post = respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"key": "PLAT-1", "id": "1"}))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(
            AssigneeTicket(summary="boom", watchers=["alice@example.com", "acc-raw", "bob@example.com"]),
            project="PLAT",
        )
        await session.commit()
    body = json.loads(post.calls[0].request.read())
    assert body["fields"]["customfield_10051"] == [
        {"accountId": "acc-aaa"},
        {"accountId": "acc-raw"},  # non-email values pass through
        {"accountId": "acc-bbb"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_email_lookup_is_cached_across_issues():
    """One lookup per email per session/engine, not per issue."""
    search = _mock_user_search()
    respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"key": "PLAT-1", "id": "1"}))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(AssigneeTicket(summary="a", assignee="alice@example.com"), project="PLAT")
        session.add(AssigneeTicket(summary="b", assignee="alice@example.com"), project="PLAT")
        await session.commit()
    assert search.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_ids_pass_through_without_a_lookup():
    search = _mock_user_search()
    post = respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"key": "PLAT-1", "id": "1"}))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(AssigneeTicket(summary="boom", assignee="acc-123"), project="PLAT")
        await session.commit()
    assert not search.called
    body = json.loads(post.calls[0].request.read())
    assert body["fields"]["assignee"] == {"accountId": "acc-123"}


# ── updates ──────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_update_resolves_dirty_assignee_email():
    _mock_user_search()
    issue_payload = {
        "key": "PLAT-1",
        "fields": {"summary": "boom", "assignee": {"accountId": "acc-old"}},
    }
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(200, json=issue_payload))
    put = respx.put(f"{CLOUD_ROOT}/issue/PLAT-1").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        ticket = await session.get(AssigneeTicket, "PLAT-1")
        assert ticket is not None
        ticket.assignee = "alice@example.com"
        await session.commit()
    body = json.loads(put.calls[0].request.read())
    assert body["fields"] == {"assignee": {"accountId": "acc-aaa"}}
    # Read-modify-write consistency: the instance now carries the accountId,
    # same shape hydration fills, so comparisons do not flap.
    assert ticket.assignee == "acc-aaa"


# ── failure mode ─────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_unresolvable_email_fails_that_issue_without_dropping_the_value():
    """No match: the commit for THAT issue fails naming the field and email;
    other issues in the batch still commit (partial-commit semantics)."""
    search = respx.get(f"{CLOUD_ROOT}/user/search").mock(
        side_effect=[
            httpx.Response(200, json=[]),  # no user for ghost@example.com
            httpx.Response(200, json=_SEARCH_RESULT),  # alice resolves fine
        ]
    )
    respx.post(f"{CLOUD_ROOT}/issue").mock(return_value=httpx.Response(201, json={"key": "PLAT-1", "id": "1"}))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(AssigneeTicket(summary="first", assignee="ghost@example.com"), project="PLAT")
        session.add(AssigneeTicket(summary="second", assignee="alice@example.com"), project="PLAT")
        with pytest.raises(PartialCommitError) as excinfo:
            await session.commit()
    assert isinstance(excinfo.value, PartialCommitError)
    failures = excinfo.value.failures
    assert len(failures) == 1
    message = str(failures[0].error)
    assert "assignee" in message and "ghost@example.com" in message
    # The value was NOT silently dropped: the failed instance keeps the email.
    assert failures[0].instance.assignee == "ghost@example.com"
    assert search.call_count == 2  # ghost failed (uncached), alice resolved


@respx.mock
@pytest.mark.asyncio
async def test_ambiguous_email_fails_naming_field_and_email():
    respx.get(f"{CLOUD_ROOT}/user/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                _user("acc-1", "one@example.com"),
                _user("acc-2", "two@example.com"),
            ],
        )
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        session.add(AssigneeTicket(summary="boom", approver="same@example.com"), project="PLAT")
        with pytest.raises(PartialCommitError) as excinfo:
            await session.commit()
    failures = excinfo.value.failures
    message = str(failures[0].error)
    assert "approver" in message and "same@example.com" in message
