"""Tell the agent where every attached file sits and what of it reached the model.

The adapter stores each attachment the Portal composer uploads under the
workspace (``uploads/<session>/<file_id>/<name>``). The chat model reads the
text-like files the adapter parses as an excerpt cut to a budget, and OpenCode
takes PDFs (and images, while the chat provider can see them; see
``vision.py``) as file parts. Everything else the model cannot read, images
for a provider without vision and the office formats the adapter does not
parse (doc, dot, xls), reaches it not at all. Whatever the type, the member
may ask for the file to be processed (converted, computed over, turned into a
new file), which takes the file itself rather than an excerpt, so every
attachment is listed with its workspace path in an "Attached files" block and
the agent is told to read it from there with its tools.

This runtime has no browser tool, so for the files the model cannot read the
block says they cannot be analyzed in this chat. The native runtime's block
(its ``src/gateway/attachment_handoff.py``) has the same header, file lines
and rule, and a wording that points at the attached-files analysis skill
(Productivity Suite through the member's own browser, the Local bridge
connector); keep the shared parts in step.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

FORWARDED_MIME_TYPES = frozenset({"application/msword", "application/vnd.ms-excel"})

ATTACHED_FILES_HEADER = (
    "Attached files: the user attached these to this message; each one is stored in the workspace at the path shown:"
)
ATTACHED_FILES_RULE = (
    "An excerpt in this prompt is a preview of a parsed file and may be cut short. To read a whole file, to process "
    "it (convert, filter, compute over it, or produce a new file from it), or to answer what the excerpt does not "
    "cover, read the file from its workspace path with your file and shell tools; never ask the user where the file "
    "is or to upload it again."
)
HANDOFF_HEADER = (
    "The chat model cannot read the files marked above as not readable (images, and office formats the runtime "
    "does not parse)."
)
HANDOFF_NO_BROWSER_TOOL = (
    "This assistant runs on the OpenCode runtime, which has no browser tool, so these files cannot be analyzed "
    "here: tell the user which files you could not read and that an assistant on the native runtime with the "
    "Local bridge switched on can analyze them (Productivity Suite through the user's own browser), never guess "
    "what a file shows, never fall back to OCR or image-parsing scripts, and continue with what the message says "
    "in words."
)

# What a listed file says about itself after its type and size: what of it
# reached the model in this prompt. ``unreadable`` files get the handoff
# wording as well.
STATUS_NOTES = {
    "excerpt": "an excerpt of it is in this prompt",
    "not_parsed": "it could not be parsed, read it from its path",
    "omitted": "left out of this prompt for size, read it from its path",
    "file_part": "sent to the model as a file part",
    "unreadable": "not readable by the chat model",
}


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
    note = STATUS_NOTES.get(str(item.get("status") or ""))
    tail = f"; {note}" if note else ""
    return f"- {path} ({', '.join(details)}{tail})"


def build_attachment_handoff(files: Iterable[Mapping[str, Any]]) -> str:
    """The prompt block that lists every attachment with its workspace path.

    Each item carries ``path``, ``content_type``, ``size_bytes``, the member's
    ``name`` and a ``status`` from ``STATUS_NOTES``. The files marked
    ``unreadable`` add the wording that this runtime cannot analyze them.
    """
    items = [item for item in files if str(item.get("path") or "").strip()]
    if not items:
        return ""
    parts = [ATTACHED_FILES_HEADER, "\n".join(describe_attached_file(item) for item in items), ATTACHED_FILES_RULE]
    if any(str(item.get("status") or "") == "unreadable" for item in items):
        parts.append(f"{HANDOFF_HEADER} {HANDOFF_NO_BROWSER_TOOL}")
    return "\n".join(parts)
