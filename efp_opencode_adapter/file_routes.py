from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web
from .app_keys import SETTINGS_KEY, FILE_SERVICE_KEY, ATTACHMENT_SERVICE_KEY

from .attachment_service import AttachmentService
from .file_service import WorkspaceFileService


@dataclass(frozen=True)
class UploadedPart:
    field_name: str
    filename: str
    content_type: str | None
    data: bytes


def _error(exc: Exception) -> web.Response:
    if isinstance(exc, PermissionError):
        return web.json_response({"success": False, "error": str(exc)}, status=403)
    if isinstance(exc, FileNotFoundError):
        return web.json_response({"success": False, "error": "not_found"}, status=404)
    if isinstance(exc, ValueError):
        msg = str(exc)
        status = 415 if msg == "unsupported_file_type" else 400
        return web.json_response({"success": False, "error": msg}, status=status)
    return web.json_response({"success": False, "error": str(exc)}, status=500)


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in {"1", "true", "yes", "on"}


async def _multipart_upload(request: web.Request) -> tuple[UploadedPart | None, dict[str, str]]:
    reader = await request.multipart()
    fields: dict[str, str] = {}
    upload: UploadedPart | None = None

    while True:
        part = await reader.next()
        if part is None:
            break

        if part.filename:
            data = await part.read(decode=False)
            candidate = UploadedPart(
                field_name=part.name or "",
                filename=part.filename or "upload.bin",
                content_type=part.headers.get("Content-Type"),
                data=data,
            )
            if upload is None or candidate.field_name == "file":
                upload = candidate
            continue

        if part.name:
            fields[part.name] = await part.text()

    return upload, fields


def register_file_routes(app: web.Application) -> None:
    settings = app[SETTINGS_KEY]
    file_service = app.get(FILE_SERVICE_KEY) or WorkspaceFileService(settings)
    attachment_service = app.get(ATTACHMENT_SERVICE_KEY) or AttachmentService(settings)
    app[FILE_SERVICE_KEY] = file_service
    app[ATTACHMENT_SERVICE_KEY] = attachment_service

    async def server_files_browse(request):
        try:
            return web.json_response(file_service.list_files(request.query.get("path") or "."))
        except Exception as exc:
            return _error(exc)

    async def server_files_read(request):
        try:
            return web.json_response(file_service.read_file(request.query.get("path") or "."))
        except Exception as exc:
            return _error(exc)

    async def server_files_content(request):
        try:
            return web.FileResponse(file_service.get_content_path(request.query.get("path") or "."))
        except Exception as exc:
            return _error(exc)

    async def server_files_upload(request):
        try:
            upload, fields = await _multipart_upload(request)
            if upload is None:
                raise ValueError("file is required")
            directory = request.query.get("directory") or request.query.get("path") or fields.get("directory") or fields.get("path") or "."
            unzip_requested = _truthy(request.query.get("unzip") or fields.get("unzip"))
            is_zip_filename = upload.filename.lower().endswith(".zip")
            if unzip_requested or is_zip_filename:
                return web.json_response(file_service.extract_zip_safely(directory, upload.filename, upload.data))
            return web.json_response(file_service.upload_file(directory, upload.filename, upload.data))
        except Exception as exc:
            return _error(exc)

    async def server_files_delete(request):
        try:
            payload = {}
            if request.content_type.startswith("application/json"):
                payload = await request.json()
            elif request.content_type.startswith("multipart/") or request.content_type.startswith("application/x-www-form-urlencoded"):
                payload = dict(await request.post())
            raw_paths = payload.get("paths")
            if raw_paths is not None:
                if not isinstance(raw_paths, list) or not raw_paths:
                    raise ValueError("paths is required")
                paths = []
                for item in raw_paths:
                    if not isinstance(item, str) or not item.strip():
                        raise ValueError("paths must contain non-empty strings")
                    paths.append(item.strip())
                return web.json_response(file_service.delete_paths(paths, recursive=True))

            path = payload.get("path") or request.query.get("path")
            if not path:
                raise ValueError("paths is required")
            recursive = _truthy(str(payload.get("recursive") or request.query.get("recursive") or "false"))
            return web.json_response(file_service.delete_path(path, recursive=recursive))
        except PermissionError as exc:
            return _error(exc)
        except OSError as exc:
            return _error(ValueError(str(exc)))
        except Exception as exc:
            return _error(exc)

    async def server_files_download(request):
        try:
            paths = request.query.getall("paths", [])
            if not paths:
                single = request.query.get("path")
                if single:
                    paths = [single]
            if not paths:
                paths = ["."]
            if len(paths) == 1:
                file_path, filename, content_type = file_service.prepare_download(paths[0])
            else:
                file_path, filename, content_type = file_service.prepare_download_many(paths)
            resp = web.FileResponse(file_path)
            resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            if content_type:
                resp.content_type = content_type
            return resp
        except Exception as exc:
            return _error(exc)

    async def attachments_upload(request):
        try:
            upload, fields = await _multipart_upload(request)
            if upload is None:
                raise ValueError("file is required")
            session_id = request.query.get("session_id") or fields.get("session_id")
            return web.json_response(
                attachment_service.upload(session_id, upload.filename, upload.data, upload.content_type)
            )
        except Exception as exc:
            return _error(exc)

    async def attachments_parse(request):
        try:
            payload = {}
            if request.content_type.startswith("application/json"):
                payload = await request.json()
            elif request.content_type.startswith("multipart/") or request.content_type.startswith("application/x-www-form-urlencoded"):
                payload = dict(await request.post())
            file_id = payload.get("file_id") or request.query.get("file_id")
            if not file_id:
                raise ValueError("file_id is required")
            session_id = payload.get("session_id") or request.query.get("session_id")
            result = attachment_service.parse(file_id, session_id)
            if not result.get("success") and result.get("error") == "unsupported_file_type":
                return web.json_response(result, status=415)
            return web.json_response(result)
        except Exception as exc:
            return _error(exc)

    async def attachments_list(request):
        try:
            return web.json_response(attachment_service.list_files(request.query.get("session_id")))
        except Exception as exc:
            return _error(exc)

    async def attachments_download(request):
        try:
            file_id = request.query.get("file_id") or request.match_info.get("file_id")
            if not file_id:
                raise ValueError("file_id is required")
            p, meta = attachment_service.download_path(file_id, request.query.get("session_id"))
            resp = web.FileResponse(p)
            resp.headers["Content-Disposition"] = f'attachment; filename="{meta["name"]}"'
            return resp
        except Exception as exc:
            return _error(exc)

    async def attachments_preview(request):
        try:
            result = attachment_service.preview(request.match_info.get("file_id") or request.query.get("file_id"), request.query.get("session_id"))
            if not result.get("success") and result.get("error") == "unsupported_file_type":
                return web.json_response(result, status=415)
            return web.json_response(result)
        except Exception as exc:
            return _error(exc)

    async def attachments_get(request):
        try:
            p, meta = attachment_service.download_path(request.match_info.get("file_id") or request.query.get("file_id"), request.query.get("session_id"))
            resp = web.FileResponse(p)
            resp.headers["Content-Disposition"] = f'inline; filename="{meta["name"]}"'
            return resp
        except Exception as exc:
            return _error(exc)

    async def attachments_delete(request):
        try:
            return web.json_response(attachment_service.delete(request.match_info.get("file_id") or request.query.get("file_id"), request.query.get("session_id")))
        except Exception as exc:
            return _error(exc)

    async def context_files(request):
        try:
            return web.json_response(attachment_service.context_files(request.query.get("session_id")))
        except Exception as exc:
            return _error(exc)

    async def chunks_search(request):
        try:
            q = request.query.get("q") if request.query.get("q") is not None else request.query.get("query", "")
            top_k = int(request.query.get("top_k", "5"))
            return web.json_response(attachment_service.search_chunks(request.query.get("session_id"), q, top_k))
        except Exception as exc:
            return _error(exc)

    app.router.add_get("/api/server-files", server_files_browse)
    app.router.add_get("/api/server-files/read", server_files_read)
    app.router.add_get("/api/server-files/content", server_files_content)
    app.router.add_post("/api/server-files/upload", server_files_upload)
    app.router.add_post("/api/server-files/delete", server_files_delete)
    app.router.add_get("/api/server-files/download", server_files_download)
    app.router.add_get("/api/files", server_files_browse)
    app.router.add_get("/api/files/read", server_files_read)
    app.router.add_post("/api/files/upload", attachments_upload)
    app.router.add_post("/api/files/parse", attachments_parse)
    app.router.add_get("/api/files/list", attachments_list)
    app.router.add_get("/api/files/download", attachments_download)
    app.router.add_get("/api/context/files", context_files)
    app.router.add_get("/api/chunks/search", chunks_search)
    app.router.add_get("/api/files/{file_id}/preview", attachments_preview)
    app.router.add_get("/api/files/{file_id}", attachments_get)
    app.router.add_delete("/api/files/{file_id}", attachments_delete)
