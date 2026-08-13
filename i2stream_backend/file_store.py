"""文件注册/查找/删除/inbox 上传/元数据/URL 构造。

依赖方向单向：仅依赖 file_state，不被 sse_proxy 反向调用。
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse

from auth import TOKEN_PATTERN
from config import settings
from file_state import (
    MEDIA_PATTERN,
    file_records,
    inbox_file_records,
    inbox_lock,
)
from models import FileRecord


def extract_media_file_events(event: dict[str, Any], seen_media_paths: set[str]) -> list[dict[str, Any]]:
    text = event_text(event)
    if not text:
        return []

    events: list[dict[str, Any]] = []
    for match in MEDIA_PATTERN.finditer(text):
        raw_path = match.group("path").strip().strip("`\"'").strip()
        if not raw_path or raw_path in seen_media_paths:
            continue
        if not looks_like_complete_media_path(raw_path):
            continue
        seen_media_paths.add(raw_path)
        try:
            record = register_local_file(Path(raw_path))
        except FileNotFoundError:
            events.append({"type": "proxy.file_error", "path": raw_path, "message": "file not found"})
            continue
        except OSError as exc:
            events.append({"type": "proxy.file_error", "path": raw_path, "message": str(exc)})
            continue

        events.append(
            {
                "type": "proxy.file",
                "file": {
                    "token": record.token,
                    "name": record.filename,
                    "url": build_file_url(record.token),
                    "media_type": record.media_type,
                },
            }
        )
    return events


def looks_like_complete_media_path(raw_path: str) -> bool:
    path = Path(raw_path)
    name = path.name
    return bool(name and "." in name and path.suffix)


def event_text(event: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("delta", "text", "output_text", "content"):
        value = event.get(key)
        if isinstance(value, str):
            pieces.append(value)

    item = event.get("item")
    if isinstance(item, dict):
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                pieces.append(content["text"])
    return "\n".join(pieces)


def register_local_file(source_path: Path) -> FileRecord:
    return register_local_file_in_store(source_path, settings.file_store_dir, file_records)


def register_local_file_in_store(
    source_path: Path,
    store_dir: Path,
    records: dict[str, FileRecord],
) -> FileRecord:
    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(str(source_path))

    store_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    safe_name = Path(resolved_source.name).name
    target_path = store_dir / f"{token}-{safe_name}"
    shutil.copyfile(resolved_source, target_path)

    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    record = FileRecord(token=token, filename=safe_name, path=target_path, media_type=media_type)
    records[token] = record
    return record


def extract_media_path(text: str) -> Path:
    match = MEDIA_PATTERN.search(text)
    if match is None:
        raise HTTPException(status_code=400, detail="Request body must contain MEDIA:/path/to/file")

    raw_path = match.group("path").strip().strip("`\"'").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="MEDIA path is empty")
    return Path(raw_path)


def require_description(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(status_code=400, detail="description is required")
    return value.strip()


async def save_upload_file(file: UploadFile, store_dir: Path, records: dict[str, FileRecord]) -> FileRecord:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    store_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    safe_name = Path(file.filename).name
    target_path = store_dir / f"{token}-{safe_name}"

    with target_path.open("wb") as target:
        while chunk := await file.read(1024 * 1024):
            target.write(chunk)

    media_type = file.content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    record = FileRecord(token=token, filename=safe_name, path=target_path, media_type=media_type)
    records[token] = record
    return record


async def resolve_inbox_upload(request: Any) -> FileRecord:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type:
        raise HTTPException(status_code=415, detail="multipart upload is not supported; send MEDIA path and description")

    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        media_text = first_form_text(form, ("media", "path", "file", "content", "input"))
        description = require_description(form.get("description") if isinstance(form.get("description"), str) else None)
        return register_inbox_media_path(media_text, description)

    if "application/json" in content_type:
        body = await request.json()
        media_text, description = inbox_upload_from_json(body)
        return register_inbox_media_path(media_text, description)

    body_text = (await request.body()).decode("utf-8", errors="replace")
    description = require_description(request.headers.get("x-description"))
    return register_inbox_media_path(body_text, description)


def first_form_text(form: Any, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in form.keys():
        if isinstance(key, str) and key.strip().startswith("MEDIA:"):
            return key
        value = form.get(key)
        if isinstance(value, str) and value.strip().startswith("MEDIA:"):
            return value
    if len(form) == 0:
        return ""
    raise HTTPException(status_code=400, detail="Form body must include MEDIA path in media, path, file, content, or input")


def inbox_upload_from_json(body: Any) -> tuple[str, str]:
    if isinstance(body, str):
        raise HTTPException(status_code=400, detail="JSON body must include media and description")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    if "report" in body:
        report = body.get("report")
        summary = body.get("summary")
        if not isinstance(report, str) or not report.strip():
            raise HTTPException(status_code=400, detail="report must be a non-empty MEDIA path string")
        if not isinstance(summary, str):
            raise HTTPException(status_code=400, detail="summary must be a string")
        for field in ("error", "warning"):
            value = body.get(field)
            if type(value) is not int or value not in {0, 1}:
                raise HTTPException(status_code=400, detail=f"{field} must be 0 or 1")
        return report, require_description(summary)

    media_text = ""
    for key in ("media", "path", "file", "content", "input"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            media_text = value
            break
    if not media_text:
        raise HTTPException(status_code=400, detail="JSON body must include MEDIA path in media, path, file, content, or input")

    description = body.get("description")
    return media_text, require_description(description if isinstance(description, str) else None)


def register_inbox_media_path(media_text: str, description: str) -> FileRecord:
    source_path = extract_media_path(media_text)
    with inbox_lock:
        try:
            record = register_local_file_in_store(source_path, settings.inbox_file_store_dir, inbox_file_records)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"MEDIA file not found: {source_path}") from exc
        record.description = description
        write_inbox_metadata(record)
        return record


def inbox_metadata_path(token: str) -> Path:
    return settings.inbox_file_store_dir / f"{token}.json"


def write_inbox_metadata(record: FileRecord) -> None:
    metadata = {
        "token": record.token,
        "name": record.filename,
        "media_type": record.media_type,
        "description": record.description,
    }
    metadata_path = inbox_metadata_path(record.token)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def read_inbox_description(token: str) -> str:
    metadata_path = inbox_metadata_path(token)
    if not metadata_path.is_file():
        return ""
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid inbox metadata for token {token}") from exc
    description = metadata.get("description")
    return description if isinstance(description, str) else ""


def file_payload(record: FileRecord, url: str) -> dict[str, Any]:
    stat = record.path.stat()
    return {
        "token": record.token,
        "name": record.filename,
        "url": url,
        "media_type": record.media_type,
        "size": stat.st_size,
        "created_at": stat.st_mtime,
        "description": record.description,
    }


def list_report_files() -> list[dict[str, Any]]:
    with inbox_lock:
        records = list_all_file_records(settings.inbox_file_store_dir, inbox_file_records)
        files = [file_payload(record, build_inbox_file_url(record.token)) for record in records]
    files.sort(key=lambda item: item["created_at"], reverse=True)
    return files


def find_file_record(token: str) -> FileRecord | None:
    return find_file_record_in_store(token, settings.file_store_dir, file_records)


def find_file_record_in_store(token: str, store_dir: Path, records: dict[str, FileRecord]) -> FileRecord | None:
    if not store_dir.is_dir():
        return None
    matches = list(store_dir.glob(f"{token}-*"))
    if len(matches) != 1:
        return None

    path = matches[0]
    filename = path.name.removeprefix(f"{token}-")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    description = read_inbox_description(token) if is_inbox_store(store_dir) else ""
    record = FileRecord(token=token, filename=filename, path=path, media_type=media_type, description=description)
    records[token] = record
    return record


def delete_inbox_record(token: str) -> bool:
    deleted = False
    record = inbox_file_records.pop(token, None) or find_file_record_in_store(
        token,
        settings.inbox_file_store_dir,
        inbox_file_records,
    )
    if record is not None:
        path = record.path
        if path.exists():
            path.unlink()
            deleted = True
        inbox_file_records.pop(token, None)

    metadata_path = inbox_metadata_path(token)
    if metadata_path.exists():
        metadata_path.unlink()
        deleted = True
    return deleted


def list_all_file_records(store_dir: Path, records: dict[str, FileRecord]) -> list[FileRecord]:
    if not store_dir.is_dir():
        return []

    discovered: list[FileRecord] = []
    for path in store_dir.iterdir():
        if not path.is_file():
            continue
        token, separator, filename = path.name.partition("-")
        if not separator or not TOKEN_PATTERN.fullmatch(token):
            continue
        record = records.get(token)
        if record is None:
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            description = read_inbox_description(token) if is_inbox_store(store_dir) else ""
            record = FileRecord(token=token, filename=filename, path=path, media_type=media_type, description=description)
            records[token] = record
        elif is_inbox_store(store_dir):
            record.description = read_inbox_description(token)
        discovered.append(record)
    return discovered


def is_inbox_store(store_dir: Path) -> bool:
    return store_dir.resolve() == settings.inbox_file_store_dir.resolve()


def file_response_from_store(record: FileRecord, store_dir: Path) -> FileResponse:
    resolved_store = store_dir.resolve()
    resolved_file = record.path.resolve()
    if not resolved_file.is_relative_to(resolved_store):
        raise HTTPException(status_code=403, detail="File path is outside the store")
    if not resolved_file.is_file():
        raise HTTPException(status_code=404, detail="File no longer exists")

    if is_html_file(record):
        return FileResponse(
            resolved_file,
            media_type="text/html",
            headers={
                "Content-Disposition": "inline",
                "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; form-action 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return FileResponse(resolved_file, media_type=record.media_type, filename=record.filename)


def is_html_file(record: FileRecord) -> bool:
    return record.media_type == "text/html" or record.filename.lower().endswith((".html", ".htm"))


def build_file_url(token: str) -> str:
    if settings.public_base_url:
        return f"{settings.public_base_url.rstrip('/')}/api/generated-files/{token}/content"
    return f"/api/generated-files/{token}/content"


def build_inbox_file_url(token: str) -> str:
    if settings.public_base_url:
        return f"{settings.public_base_url.rstrip('/')}/api/reports/{token}/content"
    return f"/api/reports/{token}/content"
