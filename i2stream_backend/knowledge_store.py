from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import HTTPException, UploadFile

from config import settings
from knowledge_config_store import get_knowledge_configuration
from models import KnowledgeServiceConfigurationRequest


SUPPORTED_KNOWLEDGE_EXTENSIONS = {"txt", "md", "pdf", "docx", "xls", "xlsx"}
TASK_TERMINAL_STATUSES = {"completed", "failed"}
QDRANT_HTTP_PORT = 6335
QDRANT_SCROLL_LIMIT = 256
KNOWLEDGE_CHECK_TIMEOUT_SECONDS = 10.0
MAX_COLLECTION_NAME_LENGTH = 128


def validate_collection_name(collection_name: str) -> str:
    if not isinstance(collection_name, str):
        raise HTTPException(status_code=400, detail="collection_name must be a string")
    normalized = collection_name.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="collection_name is required")
    if len(normalized) > MAX_COLLECTION_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"collection_name must be at most {MAX_COLLECTION_NAME_LENGTH} characters",
        )
    return normalized


def response_json_object(response: httpx.Response, description: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"{description} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"{description} must be a JSON object")
    return payload


async def upload_knowledge_file(
    file: UploadFile,
    collection_name: str | None = None,
) -> dict[str, Any]:
    normalized_collection = (
        validate_collection_name(collection_name) if collection_name is not None else None
    )
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

    vector_search_host = runtime_vector_search_host()
    return await submit_vector_file(
        file,
        filename,
        vector_search_host,
        normalized_collection,
    )


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


def runtime_vector_search_host() -> str:
    configuration = get_knowledge_configuration()
    if configuration is None:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    vector_search_host = configuration["vector_search_host"]
    if not isinstance(vector_search_host, str) or not vector_search_host:
        raise RuntimeError("Persisted knowledge configuration is invalid")
    return vector_search_host


async def submit_vector_file(
    file: UploadFile,
    filename: str,
    vector_search_host: str,
    collection_name: str | None,
) -> dict[str, Any]:
    url = f"{vector_search_host}/api/v1/upload_file"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    files = {"file": (filename, file.file, media_type)}
    async with httpx.AsyncClient(timeout=timeout) as client:
        request_arguments: dict[str, Any] = {"files": files}
        if collection_name is not None:
            request_arguments["data"] = {"collection_name": collection_name}
        response = await client.post(url, **request_arguments)
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


async def get_vector_task_status(
    task_id: str,
    collection_name: str | None = None,
) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id.strip():
        raise HTTPException(status_code=400, detail="task_id is required")

    normalized_collection = (
        validate_collection_name(collection_name) if collection_name is not None else None
    )
    vector_search_host = runtime_vector_search_host()
    url = f"{vector_search_host}/api/v1/tasks/{quote(task_id.strip(), safe='')}"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        request_arguments = {}
        if collection_name is not None:
            request_arguments["params"] = {"collection_name": normalized_collection}
        response = await client.get(url, **request_arguments)
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


def validate_collection_info(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise HTTPException(status_code=502, detail="Vector service collection item must be an object")

    name = item.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_COLLECTION_NAME_LENGTH:
        raise HTTPException(status_code=502, detail="Vector service collection item has invalid name")
    for field in ("points_count", "vectors_count"):
        value = item.get(field)
        if type(value) is not int or value < 0:
            raise HTTPException(
                status_code=502,
                detail=f"Vector service collection item has invalid {field}",
            )
    for field in ("status", "schema"):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            raise HTTPException(
                status_code=502,
                detail=f"Vector service collection item has invalid {field}",
            )
    if type(item.get("is_default")) is not bool:
        raise HTTPException(
            status_code=502,
            detail="Vector service collection item has invalid is_default",
        )
    return {
        "name": name.strip(),
        "points_count": item["points_count"],
        "vectors_count": item["vectors_count"],
        "status": item["status"],
        "schema": item["schema"],
        "is_default": item["is_default"],
    }


async def list_vector_collections(vector_search_host: str | None = None) -> list[dict[str, Any]]:
    if vector_search_host is None:
        vector_search_host = runtime_vector_search_host()
    url = f"{vector_search_host}/api/v1/collections"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    payload = response_json_object(response, "Vector service collection response")
    if payload.get("success") is not True:
        raise HTTPException(status_code=502, detail="Vector service collection response missing success=true")
    collections = payload.get("collections")
    total = payload.get("total")
    if not isinstance(collections, list):
        raise HTTPException(status_code=502, detail="Vector service collection response missing collections")
    if type(total) is not int or total < 0 or total != len(collections):
        raise HTTPException(status_code=502, detail="Vector service collection response has invalid total")
    return [validate_collection_info(item) for item in collections]


async def default_vector_collection_name(vector_search_host: str | None = None) -> str:
    collections = await list_vector_collections(vector_search_host)
    default_names = [item["name"] for item in collections if item["is_default"]]
    if len(default_names) != 1:
        raise HTTPException(
            status_code=502,
            detail="Vector service must report exactly one default collection",
        )
    return default_names[0]


async def create_vector_collection(collection_name: str) -> dict[str, str]:
    normalized_collection = validate_collection_name(collection_name)
    vector_search_host = runtime_vector_search_host()
    url = f"{vector_search_host}/api/v1/collections"
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json={"collection_name": normalized_collection})
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    payload = response_json_object(response, "Vector service create collection response")
    if payload.get("success") is not True:
        raise HTTPException(
            status_code=502,
            detail="Vector service create collection response missing success=true",
        )
    returned_name = payload.get("collection_name")
    message = payload.get("message")
    if returned_name != normalized_collection:
        raise HTTPException(
            status_code=502,
            detail="Vector service create collection response has unexpected collection_name",
        )
    if not isinstance(message, str) or not message:
        raise HTTPException(
            status_code=502,
            detail="Vector service create collection response missing message",
        )
    return {"collection_name": returned_name, "message": message}


def qdrant_base_url_from_vector_host(vector_search_host: str) -> str:
    parsed = urlparse(vector_search_host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=500, detail="Saved vector_search_host is invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{QDRANT_HTTP_PORT}"


def display_name_from_file_id(file_id: str) -> str:
    prefix, separator, suffix = file_id.partition("_")
    if separator and prefix.isdigit() and suffix:
        return suffix
    return file_id


async def list_vector_files(collection_name: str | None = None) -> list[dict[str, Any]]:
    vector_search_host = runtime_vector_search_host()
    normalized_collection = (
        validate_collection_name(collection_name)
        if collection_name is not None
        else await default_vector_collection_name(vector_search_host)
    )
    qdrant_base_url = qdrant_base_url_from_vector_host(vector_search_host)
    encoded_collection = quote(normalized_collection, safe="")
    url = f"{qdrant_base_url}/collections/{encoded_collection}/points/scroll"
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
                        "collection_name": normalized_collection,
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


async def delete_vector_file(
    file_id: str,
    collection_name: str | None = None,
) -> dict[str, Any]:
    if not isinstance(file_id, str) or not file_id.strip():
        raise HTTPException(status_code=400, detail="file_id is required")

    vector_search_host = runtime_vector_search_host()
    normalized_file_id = file_id.strip()
    default_collection = await default_vector_collection_name(vector_search_host)
    normalized_collection = (
        validate_collection_name(collection_name)
        if collection_name is not None
        else default_collection
    )
    url = f"{vector_search_host}/api/v1/files/{quote(normalized_file_id, safe='')}"

    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        request_arguments = {}
        if normalized_collection != default_collection:
            request_arguments["params"] = {"collection_name": normalized_collection}
        response = await client.delete(url, **request_arguments)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    payload = response_json_object(response, "Vector service delete response")
    deleted_chunks = payload.get("deleted_chunks")
    message = payload.get("message")
    if payload.get("success") is not True:
        raise HTTPException(status_code=502, detail="Vector service delete response missing success=true")
    if type(deleted_chunks) is not int or deleted_chunks < 0:
        raise HTTPException(status_code=502, detail="Vector service delete response has invalid deleted_chunks")
    if not isinstance(message, str) or not message:
        raise HTTPException(status_code=502, detail="Vector service delete response missing message")
    return {
        "file_id": normalized_file_id,
        "collection_name": normalized_collection,
        "deleted_chunks": deleted_chunks,
        "message": message,
    }


async def check_knowledge_service_connections(
    configuration: KnowledgeServiceConfigurationRequest,
) -> dict[str, dict[str, str]]:
    vector_health_url = f"{configuration.vector_search_host}/health"
    timeout = httpx.Timeout(KNOWLEDGE_CHECK_TIMEOUT_SECONDS)
    current_service = "Vector service"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            vector_response = await client.get(vector_health_url)
            if vector_response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Vector service health check returned HTTP "
                        f"{vector_response.status_code}"
                    ),
                )

            current_service = "RAG MCP service"
            mcp_response = await client.head(
                configuration.rag_service_mcp_url,
                headers={"Accept": "application/json, text/event-stream"},
            )
            if mcp_response.status_code >= 400 and mcp_response.status_code != 405:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "RAG MCP service check returned HTTP "
                        f"{mcp_response.status_code}"
                    ),
                )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{current_service} connection failed: {exc}",
        ) from exc

    return {
        "vector_search": {
            "status": "reachable",
            "url": vector_health_url,
        },
        "rag_service_mcp": {
            "status": "reachable",
            "url": configuration.rag_service_mcp_url,
        },
    }
