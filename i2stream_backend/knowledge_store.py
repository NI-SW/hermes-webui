from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, UploadFile

from config import settings


SUPPORTED_KNOWLEDGE_EXTENSIONS = {"txt", "md", "pdf", "docx", "xls", "xlsx"}
TASK_TERMINAL_STATUSES = {"completed", "failed"}
QDRANT_COLLECTION_NAME = "documents"
QDRANT_HTTP_PORT = 6335
QDRANT_SCROLL_LIMIT = 256


async def upload_knowledge_file(file: UploadFile) -> dict[str, Any]:
    filename = safe_upload_filename(file.filename)
    extension = file_extension(filename)
    if extension not in SUPPORTED_KNOWLEDGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported knowledge file type: {extension}. Supported: {sorted(SUPPORTED_KNOWLEDGE_EXTENSIONS)}",
        )

    size = uploaded_file_size(file)
    if size == 0:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    return await submit_vector_file(file, filename)


def safe_upload_filename(filename: str | None) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise HTTPException(status_code=400, detail="filename is required")
    safe_name = filename.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="filename is invalid")
    return safe_name


def file_extension(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def uploaded_file_size(file: UploadFile) -> int:
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    return size


async def submit_vector_file(file: UploadFile, filename: str) -> dict[str, Any]:
    url = f"{settings.vector_search_host}/api/v1/upload_file"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    files = {"file": (filename, file.file, media_type)}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, files=files)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Vector service response must be a JSON object")
    if not isinstance(payload.get("file_id"), str) or not payload["file_id"]:
        raise HTTPException(status_code=502, detail="Vector service response missing file_id")
    if not isinstance(payload.get("task_id"), str) or not payload["task_id"]:
        raise HTTPException(status_code=502, detail="Vector service response missing task_id")

    return {
        "filename": filename,
        "file_id": payload["file_id"],
        "task_id": payload["task_id"],
    }


async def get_vector_task_status(task_id: str) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id.strip():
        raise HTTPException(status_code=400, detail="task_id is required")

    url = f"{settings.vector_search_host}/api/v1/tasks/{task_id.strip()}"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Vector service task response must be a JSON object")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise HTTPException(status_code=502, detail="Vector service task response missing status")

    return {
        "task_id": payload.get("task_id", task_id.strip()),
        "status": status,
        "file_id": payload.get("file_id"),
        "file_name": payload.get("file_name"),
        "chunks_count": payload.get("chunks_count"),
        "message": payload.get("message"),
        "error": payload.get("error"),
        "terminal": status in TASK_TERMINAL_STATUSES,
    }


def qdrant_base_url_from_vector_host(vector_search_host: str) -> str:
    parsed = urlparse(vector_search_host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=500, detail="VECTOR_SEARCH_HOST is invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{QDRANT_HTTP_PORT}"


def display_name_from_file_id(file_id: str) -> str:
    prefix, separator, suffix = file_id.partition("_")
    if separator and prefix.isdigit() and suffix:
        return suffix
    return file_id


async def list_vector_files() -> list[dict[str, Any]]:
    qdrant_base_url = qdrant_base_url_from_vector_host(settings.vector_search_host)
    url = f"{qdrant_base_url}/collections/{QDRANT_COLLECTION_NAME}/points/scroll"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    offset: Any = None
    files_by_id: dict[str, dict[str, Any]] = {}

    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            request_payload: dict[str, Any] = {
                "limit": QDRANT_SCROLL_LIMIT,
                "with_payload": [
                    "file_id",
                    "file_type",
                    "file_size",
                    "upload_time",
                    "total_chunks",
                    "is_deleted",
                ],
                "with_vector": False,
                "filter": {
                    "must": [
                        {
                            "key": "is_deleted",
                            "match": {"value": "false"},
                        }
                    ]
                },
            }
            if offset is not None:
                request_payload["offset"] = offset

            response = await client.post(url, json=request_payload)
            if response.status_code >= 400:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                raise HTTPException(status_code=502, detail="Qdrant scroll response must include result")

            result = payload["result"]
            points = result.get("points")
            if not isinstance(points, list):
                raise HTTPException(status_code=502, detail="Qdrant scroll response missing points")

            for point in points:
                if not isinstance(point, dict):
                    raise HTTPException(status_code=502, detail="Qdrant point must be an object")
                point_payload = point.get("payload")
                if not isinstance(point_payload, dict):
                    raise HTTPException(status_code=502, detail="Qdrant point payload must be an object")
                file_id = point_payload.get("file_id")
                if not isinstance(file_id, str) or not file_id:
                    raise HTTPException(status_code=502, detail="Qdrant point payload missing file_id")
                current = files_by_id.get(file_id)
                total_chunks = point_payload.get("total_chunks")
                chunks_value = total_chunks if isinstance(total_chunks, int) else None
                if current is None:
                    files_by_id[file_id] = {
                        "file_id": file_id,
                        "display_name": display_name_from_file_id(file_id),
                        "file_type": point_payload.get("file_type") if isinstance(point_payload.get("file_type"), str) else "",
                        "file_size": point_payload.get("file_size") if isinstance(point_payload.get("file_size"), int) else None,
                        "upload_time": point_payload.get("upload_time") if isinstance(point_payload.get("upload_time"), str) else "",
                        "total_chunks": chunks_value,
                    }
                elif chunks_value is not None:
                    existing_chunks = current.get("total_chunks")
                    if not isinstance(existing_chunks, int) or chunks_value > existing_chunks:
                        current["total_chunks"] = chunks_value

            offset = result.get("next_page_offset")
            if offset is None:
                break

    return sorted(
        files_by_id.values(),
        key=lambda item: (str(item.get("upload_time") or ""), str(item["file_id"])),
        reverse=True,
    )


async def delete_vector_file(file_id: str) -> dict[str, Any]:
    if not isinstance(file_id, str) or not file_id.strip():
        raise HTTPException(status_code=400, detail="file_id is required")

    normalized_file_id = file_id.strip()
    qdrant_base_url = qdrant_base_url_from_vector_host(settings.vector_search_host)
    url = f"{qdrant_base_url}/collections/{QDRANT_COLLECTION_NAME}/points/delete"
    request_payload = {
        "filter": {
            "must": [
                {
                    "key": "file_id",
                    "match": {"value": normalized_file_id},
                }
            ]
        }
    }

    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=request_payload, params={"wait": "true"})
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Qdrant delete response must be a JSON object")
    result = payload.get("result")
    operation_id = result.get("operation_id") if isinstance(result, dict) else None
    return {
        "file_id": normalized_file_id,
        "operation_id": operation_id,
    }
