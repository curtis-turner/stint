"""Engine factory + URL parsing + dialect resolution."""

import pytest

from pensum import (
    ConfigurationError,
    Engine,
    PATAuth,
    create_engine,
)
from pensum.dialects.jira.cloud import JiraCloudDialect
from pensum.engine import _split_dialect_prefix


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
