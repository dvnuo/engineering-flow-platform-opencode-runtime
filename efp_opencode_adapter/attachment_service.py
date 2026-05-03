from __future__ import annotations

import json
import mimetypes
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .settings import Settings
from .state import ensure_state_dirs

_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AttachmentService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.attachments_root = ensure_state_dirs(settings).attachments_dir

    def sanitize_session_id(self, session_id: str | None) -> str:
        sid = _SESSION_RE.sub("-", (session_id or "default").strip()).strip(".-")
        return sid or "default"

    def sanitize_file_id(self, file_id: str) -> str:
        fid = (file_id or "").strip()
        if not fid or "/" in fid or "\\" in fid or ".." in fid or not _FILE_ID_RE.match(fid):
            raise ValueError("invalid file_id")
        return fid

    def sanitize_filename(self, name: str | None) -> str:
        cleaned = (name or "attachment.bin").replace("\x00", "").replace("\r", "").replace("\n", "")
        cleaned = cleaned.replace("\\", "/").split("/")[-1].strip()
        return cleaned or "attachment.bin"

    def upload(self, session_id, filename, data, content_type=None) -> dict:
        sid = self.sanitize_session_id(session_id)
        name = self.sanitize_filename(filename)
        fid = uuid.uuid4().hex
        base = self.attachments_root / sid / fid
        base.mkdir(parents=True, exist_ok=True)
        (base / "original").write_bytes(data)
        now = datetime.now(timezone.utc).isoformat()
        meta = {
            "file_id": fid,
            "session_id": sid,
            "name": name,
            "size": len(data),
            "content_type": content_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
            "created_at": now,
            "updated_at": now,
            "parsed": False,
        }
        (base / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "file_id": fid, "name": name, "size": len(data), "content_type": meta["content_type"]}

    def resolve_attachment(self, file_id, session_id=None) -> Path:
        fid = self.sanitize_file_id(file_id)
        if session_id is not None:
            sid = self.sanitize_session_id(session_id)
            path = self.attachments_root / sid / fid
            if not path.exists():
                raise FileNotFoundError
            return path
        matches = [p / fid for p in self.attachments_root.iterdir() if p.is_dir() and (p / fid).exists()]
        if not matches:
            raise FileNotFoundError
        if len(matches) > 1:
            raise ValueError("ambiguous file_id; session_id required")
        return matches[0]

    def get_metadata(self, file_id, session_id=None) -> dict:
        base = self.resolve_attachment(file_id, session_id)
        return json.loads((base / "metadata.json").read_text(encoding="utf-8"))

    def list_files(self, session_id=None) -> dict:
        sid = self.sanitize_session_id(session_id)
        sdir = self.attachments_root / sid
        files = []
        if sdir.exists():
            for p in sdir.iterdir():
                if (p / "metadata.json").exists():
                    files.append(json.loads((p / "metadata.json").read_text(encoding="utf-8")))
        return {"success": True, "session_id": sid, "files": sorted(files, key=lambda x: x.get("created_at", ""))}

    def delete(self, file_id, session_id=None) -> dict:
        base = self.resolve_attachment(file_id, session_id)
        fid = base.name
        shutil.rmtree(base)
        return {"success": True, "file_id": fid, "deleted": True}

    def download_path(self, file_id, session_id=None):
        base = self.resolve_attachment(file_id, session_id)
        return base / "original", self.get_metadata(file_id, session_id)

    def parse(self, file_id, session_id=None) -> dict:
        base = self.resolve_attachment(file_id, session_id)
        meta = self.get_metadata(file_id, session_id)
        raw = (base / "original").read_bytes()
        if not _is_supported_text(meta["name"], meta.get("content_type"), raw):
            return {"success": False, "error": "unsupported_file_type"}
        text = raw.decode("utf-8", errors="replace")
        chunks = []
        size = 2000
        for i in range(0, len(text), size):
            c = text[i : i + size]
            chunks.append({"chunk_id": f"{meta['file_id']}:{len(chunks)}", "file_id": meta["file_id"], "index": len(chunks), "content": c, "size": len(c)})
        parsed_at = datetime.now(timezone.utc).isoformat()
        payload = {"file_id": meta["file_id"], "session_id": meta["session_id"], "text": text, "chunks": chunks, "metadata": meta, "parsed_at": parsed_at}
        (base / "parsed.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        meta["parsed"] = True
        meta["updated_at"] = parsed_at
        (base / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "file_id": meta["file_id"], "text": text, "chunks": chunks, "metadata": meta}

    def preview(self, file_id, session_id=None) -> dict:
        base = self.resolve_attachment(file_id, session_id)
        meta = self.get_metadata(file_id, session_id)
        parsed_file = base / "parsed.json"
        if not parsed_file.exists():
            parsed = self.parse(file_id, session_id)
            if not parsed.get("success"):
                return parsed
            text = parsed["text"]
            chunks = parsed["chunks"]
        else:
            data = json.loads(parsed_file.read_text(encoding="utf-8"))
            text = data.get("text", "")
            chunks = data.get("chunks", [])
        return {"success": True, "file_id": meta["file_id"], "name": meta["name"], "content_type": meta["content_type"], "text": text, "preview": text[:4000], "chunks": chunks}

    def context_files(self, session_id=None) -> dict:
        listing = self.list_files(session_id)
        out = []
        for meta in listing["files"]:
            parsed_file = self.attachments_root / listing["session_id"] / meta["file_id"] / "parsed.json"
            chunk_count = total_chars = 0
            parsed_at = None
            if parsed_file.exists():
                pdata = json.loads(parsed_file.read_text(encoding="utf-8"))
                chunk_count = len(pdata.get("chunks", []))
                total_chars = len(pdata.get("text", ""))
                parsed_at = pdata.get("parsed_at")
            out.append({"file_id": meta["file_id"], "name": meta["name"], "size": meta["size"], "content_type": meta["content_type"], "parsed": bool(meta.get("parsed")), "chunk_count": chunk_count, "total_chars": total_chars, "parsed_at": parsed_at})
        return {"success": True, "session_id": listing["session_id"], "files": out}

    def search_chunks(self, session_id=None, query="", top_k=5) -> dict:
        sid = self.sanitize_session_id(session_id)
        q = (query or "").strip()
        if not q:
            return {"success": True, "session_id": sid, "query": q, "results": [], "chunks": [], "total": 0}
        matches = []
        for meta in self.list_files(sid)["files"]:
            parsed_file = self.attachments_root / sid / meta["file_id"] / "parsed.json"
            if not parsed_file.exists():
                continue
            pdata = json.loads(parsed_file.read_text(encoding="utf-8"))
            for ch in pdata.get("chunks", []):
                if q.lower() in ch.get("content", "").lower():
                    matches.append({"file_id": meta["file_id"], "name": meta["name"], "chunk_id": ch["chunk_id"], "index": ch["index"], "content": ch["content"], "score": 1.0})
        results = matches[: max(1, int(top_k or 5))]
        return {"success": True, "session_id": sid, "query": q, "results": results, "chunks": results, "total": len(results)}


def _is_supported_text(name: str, content_type: str | None, raw: bytes) -> bool:
    supported_types = {"text/plain", "text/markdown", "application/json", "text/csv", "text/tab-separated-values"}
    code_ext = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".sql", ".html", ".css", ".xml", ".dockerfile"}
    if b"\x00" in raw:
        return False
    suffix = Path(name).suffix.lower()
    if content_type in supported_types or suffix in code_ext or name in {"Dockerfile", "Makefile"}:
        return True
    if content_type and content_type.startswith("text/"):
        return True
    return False


def build_attachment_context(session_id: str, attachments: list[dict], *, settings: Settings | None = None, max_chars: int = 30000) -> str:
    if settings is None:
        settings = Settings.from_env()
    service = AttachmentService(settings)
    sid = service.sanitize_session_id(session_id)
    parts = ["Attached files:\n"]
    used = len(parts[0])
    for item in attachments:
        fid = item.get("file_id") if isinstance(item, dict) else None
        if not fid:
            continue
        try:
            meta = service.get_metadata(fid, sid)
            parsed_file = service.resolve_attachment(fid, sid) / "parsed.json"
            if not parsed_file.exists():
                continue
            text = json.loads(parsed_file.read_text(encoding="utf-8")).get("text", "")
        except (FileNotFoundError, ValueError):
            continue
        title = service.sanitize_filename(meta.get("name"))
        block = f"\n## {title}\n{text}\n"
        if used + len(block) > max_chars:
            remain = max_chars - used
            if remain > 0:
                parts.append(block[:remain])
            parts.append("\n\n[Attachment context truncated]\n")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts) if len(parts) > 1 else ""
