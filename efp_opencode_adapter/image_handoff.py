"""Hand image attachments to the agent when the chat model cannot see them.

GitHub Copilot's models no longer accept image input, so for a Copilot chat
turn the adapter leaves attached images out of the OpenCode message parts and
describes them to the agent instead: where each file is in the workspace and
how to read it with the inspect-image CLI, which runs on AI Platform. The text
is the same as the native runtime's handoff block.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

HANDOFF_HEADER = (
    "Attached image files. The chat model cannot see images, so read each file "
    "with the inspect-image CLI before answering:"
)
HANDOFF_HOWTO = (
    "For each file run `inspect-image inspect --image <path> --prompt \"<what to read or explain>\" --json` "
    "from bash and answer from data.result.answer and data.result.visible_text; add --preset ocr, ui, "
    "diagram, chart, or error when it fits, and ask a follow-up question with another call. Never pass "
    "--model, never guess what an image shows, and never fall back to OCR or image-parsing scripts."
)
HANDOFF_NOT_CONFIGURED = (
    "Image analysis is not configured for this profile, so inspect-image will answer auth_required: "
    "tell the user to turn on Image analysis and enter AI Platform credentials under Portal > Connectors, "
    "do not guess what the image shows, and continue with what the message says in words."
)


def describe_attached_image(item: Mapping[str, Any]) -> str:
    path = str(item.get("path") or "").strip()
    content_type = str(item.get("content_type") or "image").strip()
    size = item.get("size_bytes")
    name = str(item.get("name") or "").strip()
    details = [content_type]
    if isinstance(size, int) and size >= 0:
        details.append(f"{size} bytes")
    if name and name != path.rsplit("/", 1)[-1] and name != path.rsplit("\\", 1)[-1]:
        details.append(f'attached as "{name}"')
    return f"- {path} ({', '.join(details)})"


def build_image_handoff(
    files: Iterable[Mapping[str, Any]],
    *,
    image_analysis: Mapping[str, Any] | None = None,
) -> str:
    """The prompt block that replaces inlined image content."""
    lines = [describe_attached_image(item) for item in files if str(item.get("path") or "").strip()]
    if not lines:
        return ""
    configured = bool((image_analysis or {}).get("configured"))
    parts = [HANDOFF_HEADER, "\n".join(lines), HANDOFF_HOWTO if configured else HANDOFF_NOT_CONFIGURED]
    return "\n".join(parts)
