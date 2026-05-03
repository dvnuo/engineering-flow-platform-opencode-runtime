from __future__ import annotations

import io
import mimetypes
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .settings import Settings


class WorkspaceFileService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.workspace_dir.resolve()

    def resolve_workspace_path(self, user_path: str | None) -> Path:
        raw = (user_path or ".").strip()
        if raw in {"", ".", "/"}:
            return self.root
        normalized = raw.replace("\\", "/")
        candidate = PurePosixPath(normalized)
        if candidate.is_absolute():
            raise PermissionError("path outside workspace")
        resolved = (self.root / str(candidate)).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError("path outside workspace") from exc
        return resolved

    def _ensure_under_workspace(self, path: Path, *, message: str = "path outside workspace") -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(message) from exc
        return resolved

    def workspace_relative_path(self, path: Path) -> str:
        rel = self._ensure_under_workspace(path).relative_to(self.root)
        return "." if str(rel) == "." else rel.as_posix()

    def list_files(self, user_path: str | None) -> dict:
        target = self.resolve_workspace_path(user_path)
        if not target.exists():
            raise FileNotFoundError
        if not target.is_dir():
            raise ValueError("path must be a directory")
        items = []
        for entry in target.iterdir():
            if entry.is_symlink():
                continue
            stat = entry.stat()
            items.append(
                {
                    "name": entry.name,
                    "path": self.workspace_relative_path(entry),
                    "type": "directory" if entry.is_dir() else "file",
                    "size": 0 if entry.is_dir() else stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                }
            )
        items.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return {"success": True, "path": self.workspace_relative_path(target), "items": items}

    def read_file(self, user_path: str) -> dict:
        target = self.resolve_workspace_path(user_path)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError
        raw = target.read_bytes()
        content = raw.decode("utf-8", errors="replace")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return {
            "success": True,
            "path": self.workspace_relative_path(target),
            "content": content,
            "language": _guess_language(target),
            "content_type": content_type,
            "size": len(raw),
        }

    def get_content_path(self, user_path: str) -> Path:
        target = self.resolve_workspace_path(user_path)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError
        return target

    def _resolve_write_target(self, directory: str | None, filename: str) -> tuple[Path, str]:
        name = _sanitize_filename(filename)
        target_dir = self.resolve_workspace_path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_under_workspace(target_dir)
        target = target_dir / name
        if target.is_symlink():
            raise PermissionError("path outside workspace")
        if target.exists():
            self._ensure_under_workspace(target)
        return target, name

    def upload_file(self, directory: str | None, filename: str, data: bytes) -> dict:
        target, name = self._resolve_write_target(directory, filename)
        target.write_bytes(data)
        return {
            "success": True,
            "name": name,
            "path": self.workspace_relative_path(target),
            "size": len(data),
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        }

    def extract_zip_safely(self, directory: str | None, filename: str, data: bytes) -> dict:
        _sanitize_filename(filename)
        target_dir = self.resolve_workspace_path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid_zip_file") from exc

        with zf:
            members = zf.infolist()
            extracted_items: list[str] = []
            for info in members:
                member_name = info.filename.replace("\\", "/")
                posix = PurePosixPath(member_name)
                if posix.is_absolute() or ".." in posix.parts:
                    raise PermissionError("zip entry outside target directory")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise PermissionError("zip entry outside target directory")
                resolved = (target_dir / str(posix)).resolve()
                try:
                    resolved.relative_to(self.root)
                    resolved.relative_to(target_dir.resolve())
                except ValueError as exc:
                    raise PermissionError("zip entry outside target directory") from exc
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                zf.extractall(tmp_dir)
                for path in tmp_dir.rglob("*"):
                    rel = path.relative_to(tmp_dir)
                    dest = target_dir / rel
                    if path.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, dest)
                        extracted_items.append(self.workspace_relative_path(dest))
        return {"success": True, "path": self.workspace_relative_path(target_dir), "items": sorted(extracted_items)}

    def delete_path(self, user_path: str, recursive: bool = False) -> dict:
        target = self.resolve_workspace_path(user_path)
        if target == self.root:
            raise PermissionError("cannot delete workspace root")
        if not target.exists():
            raise FileNotFoundError
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        return {"success": True, "path": self.workspace_relative_path(target), "deleted": True}

    def prepare_download(self, user_path: str) -> tuple[Path, str, str | None]:
        target = self.resolve_workspace_path(user_path)
        if not target.exists():
            raise FileNotFoundError
        if target.is_file():
            return target, target.name, None
        if target.is_dir():
            rel = self.workspace_relative_path(target)
            archive_name = f"{target.name or 'workspace'}.zip"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp_path = Path(tmp.name)
            tmp.close()
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in target.rglob("*"):
                    if p.is_symlink():
                        raise PermissionError("path outside workspace")
                    if not p.is_file():
                        continue
                    resolved = self._ensure_under_workspace(p)
                    zf.write(resolved, arcname=(Path(rel) / p.relative_to(target)).as_posix() if rel != "." else p.relative_to(target).as_posix())
            return tmp_path, archive_name, "application/zip"
        raise ValueError("unsupported path")


def _sanitize_filename(filename: str) -> str:
    name = (filename or "").replace("\x00", "").replace("\r", "").replace("\n", "")
    name = name.replace("\\", "/").split("/")[-1].strip()
    if not name:
        raise ValueError("invalid filename")
    return name


def _guess_language(path: Path) -> str:
    mapping = {
        ".md": "markdown",
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".json": "json",
        ".csv": "csv",
        ".txt": "text",
    }
    return mapping.get(path.suffix.lower(), "text")
