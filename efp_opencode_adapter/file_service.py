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
        candidate = Path(normalized)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.root / normalized).resolve()
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
            try:
                if entry.is_symlink():
                    continue
                stat = entry.stat()
                is_dir = entry.is_dir()
                is_file = entry.is_file()
                items.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                    "relative_path": self.workspace_relative_path(entry),
                    "is_dir": is_dir,
                    "is_file": is_file,
                    "type": "directory" if is_dir else "file",
                    "size": 0 if is_dir else stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                })
            except (PermissionError, FileNotFoundError, OSError):
                continue
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {
            "success": True,
            "root_path": str(self.root),
            "path": str(target.resolve()),
            "relative_path": self.workspace_relative_path(target),
            "items": items,
        }

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
        relative_path = self.workspace_relative_path(target)
        return {
            "success": True,
            "name": name,
            "path": relative_path,
            "relative_path": relative_path,
            "target_path": str(target.resolve()),
            "uploaded_filename": name,
            "mode": "file_save",
            "size": len(data),
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        }

    def extract_zip_safely(self, directory: str | None, filename: str, data: bytes) -> dict:
        clean_name = _sanitize_filename(filename)
        target_dir = self.resolve_workspace_path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid_zip_file") from exc

        with zf:
            members = zf.infolist()
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

            extracted_items: list[str] = []
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                zf.extractall(tmp_dir)
                for path in tmp_dir.rglob("*"):
                    rel = path.relative_to(tmp_dir)
                    dest = target_dir / rel
                    if path.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                    extracted_items.append(self.workspace_relative_path(dest))

        relative_target = self.workspace_relative_path(target_dir)
        return {
            "success": True,
            "mode": "zip_extract",
            "uploaded_filename": clean_name,
            "path": relative_target,
            "relative_path": relative_target,
            "target_path": str(target_dir.resolve()),
            "items": sorted(extracted_items),
            "extracted_count": len(extracted_items),
        }

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

    def delete_paths(self, user_paths: list[str], *, recursive: bool = True) -> dict:
        if not user_paths:
            raise ValueError("paths is required")

        resolved_targets: list[tuple[Path, bool, bool, str]] = []
        seen: set[Path] = set()
        for user_path in user_paths:
            target = self.resolve_workspace_path(user_path)
            if target == self.root:
                raise PermissionError("cannot delete workspace root")
            if not target.exists():
                raise FileNotFoundError
            if target in seen:
                continue
            seen.add(target)
            resolved_targets.append((target, target.is_dir(), target.is_file(), self.workspace_relative_path(target)))

        resolved_targets = self._collapse_delete_targets(resolved_targets)

        deleted = []
        for target, is_dir, is_file, rel in resolved_targets:
            if is_file:
                target.unlink()
            elif is_dir:
                if recursive:
                    shutil.rmtree(target)
                else:
                    target.rmdir()
            deleted.append({"path": str(target.resolve()), "relative_path": rel, "is_dir": is_dir, "is_file": is_file})

        return {"success": True, "deleted": deleted}

    @staticmethod
    def _is_descendant(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return child != parent
        except ValueError:
            return False

    def _collapse_delete_targets(
        self, targets: list[tuple[Path, bool, bool, str]]
    ) -> list[tuple[Path, bool, bool, str]]:
        collapsed: list[tuple[Path, bool, bool, str]] = []
        for target, is_dir, is_file, rel in targets:
            if any(existing_is_dir and self._is_descendant(target, existing) for existing, existing_is_dir, _, _ in collapsed):
                continue
            if is_dir:
                collapsed = [
                    existing_tuple
                    for existing_tuple in collapsed
                    if not self._is_descendant(existing_tuple[0], target)
                ]
            collapsed.append((target, is_dir, is_file, rel))
        return collapsed

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
                    arc = (Path(rel) / p.relative_to(target)).as_posix() if rel != "." else p.relative_to(target).as_posix()
                    zf.write(resolved, arcname=arc)
            return tmp_path, archive_name, "application/zip"
        raise ValueError("unsupported path")

    def prepare_download_many(self, user_paths: list[str]) -> tuple[Path, str, str | None]:
        if not user_paths:
            raise ValueError("paths is required")
        if len(user_paths) == 1:
            return self.prepare_download(user_paths[0])

        targets = []
        for user_path in user_paths:
            target = self.resolve_workspace_path(user_path)
            if not target.exists():
                raise FileNotFoundError
            targets.append(target)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp_path = Path(tmp.name)
        tmp.close()
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for target in targets:
                if target.is_symlink():
                    raise PermissionError("path outside workspace")
                rel = self.workspace_relative_path(target)
                if target.is_file():
                    zf.write(self._ensure_under_workspace(target), arcname=rel)
                elif target.is_dir():
                    for p in target.rglob("*"):
                        if p.is_symlink():
                            raise PermissionError("path outside workspace")
                        if not p.is_file():
                            continue
                        resolved = self._ensure_under_workspace(p)
                        arc = (Path(rel) / p.relative_to(target)).as_posix() if rel != "." else p.relative_to(target).as_posix()
                        zf.write(resolved, arcname=arc)
                else:
                    raise ValueError("unsupported path")
        return tmp_path, "server-files.zip", "application/zip"


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
