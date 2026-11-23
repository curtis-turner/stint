"""Data plane: attachments (multipart upload, list, download, delete) — #47."""

from __future__ import annotations

import sys

import httpx
import pytest
import respx

from stint import AsyncSession, PATAuth, StateFile, StintError, create_engine
from stint.engine import Engine
from stint.exceptions import ConfigurationError, TransportError
from stint.registry import registry
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


def _attachment_payload(id: str = "20001", filename: str = "export.csv") -> dict:
    return {
        "id": id,
        "filename": filename,
        "size": 42,
        "created": "2026-11-23T10:00:00.000+0000",
        "content": f"{BASE}/rest/api/3/attachment/content/{id}",
    }


# ── attach ───────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_attach_uploads_multipart_with_no_check_token(tmp_path):
    csv_file = tmp_path / "export.csv"
    csv_file.write_bytes(b"record,status\n1,open\n")
    route = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/attachments").mock(
        return_value=httpx.Response(200, json=_attachment_payload())
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        attachment = await session.attach("PLAT-1", csv_file)

    assert route.called
    request = route.calls[0].request
    # Jira 403s the multipart POST without the CSRF token header.
    assert request.headers["X-Atlassian-Token"] == "no-check"
    assert "multipart/form-data" in request.headers["Content-Type"]
    assert b'filename="export.csv"' in request.content
    assert b"record,status" in request.content
    assert attachment.id == "20001"
    assert attachment.filename == "export.csv"
    assert attachment.size == 42


@respx.mock
@pytest.mark.asyncio
async def test_attach_accepts_bytes_with_explicit_filename():
    route = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/attachments").mock(
        return_value=httpx.Response(200, json=_attachment_payload())
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.attach("PLAT-1", b"raw-bytes", filename="blob.bin")
    content = route.calls[0].request.content
    assert b'filename="blob.bin"' in content
    assert b"raw-bytes" in content


@respx.mock
@pytest.mark.asyncio
async def test_attach_bytes_without_filename_is_a_local_error():
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(ConfigurationError, match="filename="):
            await session.attach("PLAT-1", b"raw-bytes")


@respx.mock
@pytest.mark.asyncio
async def test_attach_missing_path_is_a_local_error():
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(ConfigurationError, match="no such file"):
            await session.attach("PLAT-1", "/nonexistent/path/export.csv")


@respx.mock
@pytest.mark.asyncio
async def test_attach_server_errors_surface_as_stint_errors():
    """A size-limit rejection (413) or any server error must arrive as a
    StintError subclass, not a raw httpx exception."""
    respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/attachments").mock(
        return_value=httpx.Response(413, json={"errorMessages": ["too large"]})
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        with pytest.raises(StintError) as excinfo:
            await session.attach("PLAT-1", b"x" * 10, filename="big.bin")
    assert isinstance(excinfo.value, TransportError)
    assert excinfo.value.status_code == 413


# ── attachments ──────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_attachments_lists_from_one_issue_get():
    route = respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "attachment": [
                        _attachment_payload("20001", "export.csv"),
                        _attachment_payload("20002", "log.txt"),
                    ]
                },
            },
        )
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        files = await session.attachments("PLAT-1")
    assert route.called
    assert route.calls[0].request.url.params["fields"] == "attachment"
    assert [f.id for f in files] == ["20001", "20002"]
    assert [f.filename for f in files] == ["export.csv", "log.txt"]


# ── download / delete ────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_download_attachment_returns_bytes():
    respx.get(f"{CLOUD_ROOT}/attachment/content/20001").mock(
        return_value=httpx.Response(200, content=b"record,status\n1,open\n")
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        data = await session.download_attachment("20001")
    assert data == b"record,status\n1,open\n"


@respx.mock
@pytest.mark.asyncio
async def test_download_attachment_follows_cdn_redirect():
    """The content endpoint may 302 to a pre-authenticated CDN URL."""
    respx.get(f"{CLOUD_ROOT}/attachment/content/20001").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/signed"})
    )
    respx.get("https://cdn.example.com/signed").mock(return_value=httpx.Response(200, content=b"csv-bytes"))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        data = await session.download_attachment("20001")
    assert data == b"csv-bytes"


@respx.mock
@pytest.mark.asyncio
async def test_delete_attachment_hits_the_right_path():
    route = respx.delete(f"{CLOUD_ROOT}/attachment/20001").mock(return_value=httpx.Response(204))
    async with AsyncSession(_cloud_engine(), _state()) as session:
        await session.delete_attachment("20001")
    assert route.called


# ── replace_attachment ───────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_replace_attachment_deletes_same_filename_then_uploads(tmp_path):
    """The regenerated-export pattern: no stacking of export.csv copies."""
    csv_file = tmp_path / "export.csv"
    csv_file.write_bytes(b"v2\n")
    respx.get(f"{CLOUD_ROOT}/issue/PLAT-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "PLAT-1",
                "fields": {
                    "attachment": [
                        _attachment_payload("20001", "export.csv"),
                        _attachment_payload("20002", "log.txt"),  # different name: kept
                    ]
                },
            },
        )
    )
    delete = respx.delete(f"{CLOUD_ROOT}/attachment/20001").mock(return_value=httpx.Response(204))
    delete_other = respx.delete(f"{CLOUD_ROOT}/attachment/20002").mock(return_value=httpx.Response(204))
    upload = respx.post(f"{CLOUD_ROOT}/issue/PLAT-1/attachments").mock(
        return_value=httpx.Response(200, json=_attachment_payload("20003", "export.csv"))
    )
    async with AsyncSession(_cloud_engine(), _state()) as session:
        attachment = await session.replace_attachment("PLAT-1", csv_file)
    assert attachment.id == "20003"
    assert delete.called
    assert not delete_other.called  # other attachments survive
    assert upload.called
