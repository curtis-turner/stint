"""Engine factory + URL parsing + dialect resolution."""

import pytest

from stint import (
    ConfigurationError,
    Engine,
    PATAuth,
    create_engine,
)
from stint.dialects.jira.cloud import JiraCloudDialect
from stint.engine import _split_dialect_prefix, create_tmp_engine


# ── URL parsing ───────────────────────────────────────────────────────
def test_split_url_with_dialect_prefix():
    base, dialect = _split_dialect_prefix("jira_cloud+https://jira.example.com")
    assert base == "https://jira.example.com"
    assert dialect == "jira_cloud"


def test_split_url_without_prefix():
    base, dialect = _split_dialect_prefix("https://jira.example.com")
    assert base == "https://jira.example.com"
    assert dialect is None


def test_split_url_with_path():
    base, dialect = _split_dialect_prefix("jira_cloud+https://jira.example.com/jira")
    assert base == "https://jira.example.com/jira"
    assert dialect == "jira_cloud"


# ── Engine construction ───────────────────────────────────────────────
def test_create_engine_with_url_prefix():
    eng = create_engine("jira_cloud+https://jira.example.com", auth=PATAuth("tok"))
    assert isinstance(eng, Engine)
    assert isinstance(eng.dialect, JiraCloudDialect)
    assert eng.base_url == "https://jira.example.com"


def test_dialect_construction_failure_closes_the_client(monkeypatch):
    """L9: when the dialect (or Engine) constructor raises, the freshly
    built httpx pool must be closed synchronously instead of leaking until
    the ResourceWarning at GC time."""
    from stint.client.http import JiraHTTPClient

    closed = []
    real_close_sync = JiraHTTPClient.close_sync

    def _recording_close_sync(self):
        real_close_sync(self)
        closed.append(True)

    class _Boom:
        def __init__(self, client):
            raise RuntimeError("dialect boom")

    monkeypatch.setattr(JiraHTTPClient, "close_sync", _recording_close_sync)
    monkeypatch.setattr("stint.engine._CMP_DIALECT_REGISTRY", {"jira_cloud": _Boom})

    with pytest.raises(RuntimeError, match="dialect boom"):
        create_engine("https://jira.example.com", auth=PATAuth("tok"), dialect="jira_cloud")
    assert closed == [True]


def test_tmp_dialect_construction_failure_closes_the_client(monkeypatch):
    from stint.client.http import JiraHTTPClient

    closed = []
    real_close_sync = JiraHTTPClient.close_sync

    def _recording_close_sync(self):
        real_close_sync(self)
        closed.append(True)

    class _Boom:
        def __init__(self, client):
            raise RuntimeError("tmp boom")

    monkeypatch.setattr(JiraHTTPClient, "close_sync", _recording_close_sync)
    # create_tmp_engine re-imports TmpDialect from its defining module on
    # every call, so the origin module is the thing to patch.
    monkeypatch.setattr("stint.dialects.jira.tmp.dialect.TmpDialect", _Boom)

    with pytest.raises(RuntimeError, match="tmp boom"):
        create_tmp_engine("jira_cloud_tmp+https://jira.example.com", auth=PATAuth("tok"))
    assert closed == [True]


def test_create_engine_with_kwarg_dialect():
    eng = create_engine(
        "https://jira.example.com",
        auth=PATAuth("tok"),
        dialect="jira_cloud",
    )
    assert isinstance(eng.dialect, JiraCloudDialect)


def test_create_engine_missing_dialect_raises():
    with pytest.raises(ConfigurationError) as e:
        create_engine("https://jira.example.com", auth=PATAuth("tok"))
    assert "dialect" in str(e.value).lower()


def test_create_engine_unknown_dialect_raises():
    with pytest.raises(ConfigurationError) as e:
        create_engine(
            "https://jira.example.com",
            auth=PATAuth("tok"),
            dialect="oracle_form_builder",
        )
    assert "Unknown dialect" in str(e.value)


def test_create_engine_kwarg_dialect_overrides_url_prefix():
    """A kwarg dialect wins over a URL-prefix dialect when both are present.

    The URL prefix names an unknown dialect; if the kwarg did not take
    precedence, construction would raise on the unknown prefix.
    """
    eng = create_engine(
        "jira_legacy+https://acme.atlassian.net",
        auth=PATAuth("tok"),
        dialect="jira_cloud",
    )
    assert isinstance(eng.dialect, JiraCloudDialect)
