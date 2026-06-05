"""Atlassian Document Format (ADF) emission for Cloud writes.

For 0.1, only the minimal plain-text → ADF wrap is implemented. Each blank
line in the input becomes a paragraph break; lines within a paragraph join
with spaces.

ADF parsing (Cloud reads) is deferred to a later milestone.
"""

from __future__ import annotations


def wrap_plain_text(text: str) -> dict:
    """Wrap a plain string in the minimal ADF document shape Cloud accepts."""
    if not text:
        return {"type": "doc", "version": 1, "content": []}
    paragraphs: list[dict] = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": chunk}],
            }
        )
    return {"type": "doc", "version": 1, "content": paragraphs}
