"""Hand the attachments the chat model cannot read to the agent.

The chat model reads the text-like files the adapter parses, and OpenCode
takes PDFs (and images, while the chat provider can see them; see
``vision.py``) as file parts. Everything else the model cannot read, images
for a provider without vision and the office formats the adapter does not
parse (doc, dot, xls), stays under the workspace and is described to the
agent in an "Attached files" block instead.

This runtime has no browser tool, so the block says the files cannot be
analyzed in this chat. The native runtime's block (its
``src/gateway/attachment_handoff.py``) has the same header and file lines and
a wording that points at the attached-files analysis skill (Productivity
Suite through the member's own browser, the Local bridge connector); keep the
shared parts in step.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

FORWARDED_MIME_TYPES = frozenset({"application/msword", "application/vnd.ms-excel"})

HANDOFF_HEADER = (
    "Attached files the chat model cannot read (images, and office formats the runtime does not parse); "
    "each one sits in the workspace:"
)
HANDOFF_NO_BROWSER_TOOL = (
    "This assistant runs on the OpenCode runtime, which has no browser tool, so these files cannot be analyzed "
    "here: tell the user which files you could not read and that an assistant on the native runtime with the "
    "Local bridge switched on can analyze them (Productivity Suite through the user's own browser), never guess "
    "what a file shows, never fall back to OCR or image-parsing scripts, and continue with what the message says "
    "in words."
)


def is_forwarded_mime(content_type: Any) -> bool:
    """Whether a stored file is one the agent analyzes elsewhere instead of the adapter parsing it."""
    normalized = str(content_type or "").lower().split(";")[0].strip()
    return normalized in FORWARDED_MIME_TYPES


def describe_attached_file(item: Mapping[str, Any]) -> str:
    path = str(item.get("path") or "").strip()
    content_type = str(item.get("content_type") or "file").strip()
    size = item.get("size_bytes")
    name = str(item.get("name") or "").strip()
    details = [content_type]
    if isinstance(size, int) and size >= 0:
        details.append(f"{size} bytes")
    if name and name != path.rsplit("/", 1)[-1] and name != path.rsplit("\\", 1)[-1]:
        details.append(f'attached as "{name}"')
    return f"- {path} ({', '.join(details)})"


def build_attachment_handoff(files: Iterable[Mapping[str, Any]]) -> str:
    """The prompt block that stands in for the files the model cannot read."""
    lines = [describe_attached_file(item) for item in files if str(item.get("path") or "").strip()]
    if not lines:
        return ""
    return "\n".join([HANDOFF_HEADER, "\n".join(lines), HANDOFF_NO_BROWSER_TOOL])
