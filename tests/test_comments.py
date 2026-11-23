"""Data plane: comments (add, list, delete) — issue #46."""

from __future__ import annotations

import json
import sys

import httpx
import pytest
import respx

from stint import AsyncSession, PATAuth, StateFile, create_engine
from stint.engine import Engine
from stint.registry import registry
from stint.schema.issuetype import IssueType
from stint.state.file import (
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
    state.issuetypes["ticket"] = SimpleMapping(id="10010")
    state.projects["PLAT"] = ProjectMapping(id="proj-1", key="PLAT")
    return state


class Note(IssueType):
    __alias__ = "note"
    summary: str
    status: str | None = None


def _comment(id: str, text: str, author: str = "automation bot") -> dict:
    return {
        "id": id,
        "author": {"accountId": "acc-1", "displayName": author},
        "created": "2026-11-23T10:00:00.000+0000",
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        },
    }


# ── add_comment ──────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_add_comment_wraps_plain_text_in_adf_on_cloud():
    route = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/comment").mock(
        return_value=httpx.Response(201, json=_comment("10001", "x"))
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.add_comment("PLAT-1", "validation passed")
    body = json.loads(route.calls[0].request.read())
    assert body["body"]["type"] == "doc"
    assert body["body"]["content"][0]["content"][0]["text"] == "validation passed"


@respx.mock
@pytest.mark.asyncio
async def test_add_comment_accepts_raw_adf():
    route = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/comment").mock(
        return_value=httpx.Response(201, json=_comment("10001", "x"))
    )
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "raw"}],
            }
        ],
    }
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.add_comment("PLAT-1", adf)
    body = json.loads(route.calls[0].request.read())
    assert body["body"] == adf  # passed through untouched


@respx.mock
@pytest.mark.asyncio
async def test_add_comment_returns_plain_text_comment():
    respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/comment").mock(
        return_value=httpx.Response(201, json=_comment("10001", "validation passed"))
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        comment = await session.add_comment("PLAT-1", "whatever")
    assert comment.id == "10001"
    assert comment.author == "automation bot"
    assert comment.created.startswith("2026-11-23")
    assert comment.body_text == "validation passed"


@respx.mock
@pytest.mark.asyncio
async def test_add_comment_accepts_a_hydrated_instance():
    route = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/comment").mock(
        return_value=httpx.Response(201, json=_comment("10001", "x"))
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        ticket = Note(key="PLAT-1", summary="boom")
        await session.add_comment(ticket, "nag")
    assert route.called


# ── comments() ───────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_comments_lists_with_pagination_and_plain_text_bodies():
    page1 = {
        "comments": [_comment("10001", "first"), _comment("10002", "second")],
        "startAt": 0,
        "maxResults": 2,
        "total": 3,
        "isLast": False,
    }
    page2 = {
        "comments": [_comment("10003", "third")],
        "startAt": 2,
        "maxResults": 2,
        "total": 3,
        "isLast": True,
    }
    route = respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/comment").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        comments = await session.comments("PLAT-1")
    assert [c.id for c in comments] == ["10001", "10002", "10003"]
    assert [c.body_text for c in comments] == ["first", "second", "third"]
    assert route.calls[1].request.url.params["startAt"] == "2"


@respx.mock
@pytest.mark.asyncio
async def test_comments_renders_multi_paragraph_body_for_dedup():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "two"}]},
        ],
    }
    payload = _comment("10001", "ignored")
    payload["body"] = doc
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1/comment").mock(
        return_value=httpx.Response(200, json={"comments": [payload], "startAt": 0, "total": 1})
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        comments = await session.comments("PLAT-1")
    assert comments[0].body_text == "one\n\ntwo"


# ── delete_comment ───────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_delete_comment_hits_the_right_path():
    route = respx.delete(f"{CLOUD_ROOT}/issue/PLAT-1/comment/10001").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.delete_comment("PLAT-1", "10001")
    assert route.called
