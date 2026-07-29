"""Atlassian Document Format (ADF) support.

* ``wrap_plain_text`` – minimal plain‑text → ADF wrapper used by the
  Cloud write path.
* ``wrap_text`` – public alias for ``wrap_plain_text``.
* ``parse_text`` – convert a minimal ADF document back to plain text.

The parser handles paragraphs, text nodes, and ignores unknown nodes.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
#  Wrapping helpers
# ---------------------------------------------------------------------------


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


# Public alias
wrap_text = wrap_plain_text

# ---------------------------------------------------------------------------
#  Parsing helpers
# ---------------------------------------------------------------------------


def parse_text(adf: dict) -> str:
    """Parse a minimal Atlassian Document Format (ADF) dict into plain text.

    The implementation mirrors the logic in ``wrap_plain_text``: each
    paragraph is joined by spaces, and paragraphs are separated by two
    newlines. Non‑text nodes are ignored.
    """
    if not isinstance(adf, dict) or adf.get("type") != "doc":
        return ""
    paragraphs: list[str] = []
    for node in adf.get("content", []):
        if node.get("type") == "paragraph":
            texts: list[str] = []
            for leaf in node.get("content", []):
                if leaf.get("type") == "text":
                    texts.append(leaf.get("text", ""))
            paragraphs.append(" ".join(texts))
    return "\n\n".join(paragraphs)
