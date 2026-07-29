from stint.query.adf import parse_text, wrap_text
from stint.query.hydrate import hydrate
from stint.schema.issuetype import IssueType
from stint.state.file import StateFile


# Minimal issue type for testing
class SimpleIssue(IssueType):
    __alias__ = "simple"
    summary: str
    description: str | None = None


# Helper to build a minimal state file


def make_state() -> StateFile:
    return StateFile(env="test", jira_url="http://example.com", custom_fields={}, issuetypes={}, projects={})


def test_wrap_and_parse_roundtrip():
    text = "Line one\n\nLine two"
    adf = wrap_text(text)
    # ADF should contain two paragraphs
    assert adf["type"] == "doc"
    assert len(adf["content"]) == 2
    # Parsing should recover original text
    recovered = parse_text(adf)
    assert recovered == text


def test_parse_text_unknown_node():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "unknown", "content": [{"type": "text", "text": "Ignored"}]},
        ],
    }
    assert parse_text(adf) == "Hello"


def test_hydrate_description_from_adf():
    adf_doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "World"}]},
        ],
    }
    issue_payload = {
        "key": "TEST-1",
        "fields": {
            "summary": "Test summary",
            "description": adf_doc,
        },
    }
    state = make_state()
    instance = hydrate(SimpleIssue, issue_payload, state)
    assert instance.key == "TEST-1"
    assert instance.summary == "Test summary"
    assert instance.description == "Hello\n\nWorld"
