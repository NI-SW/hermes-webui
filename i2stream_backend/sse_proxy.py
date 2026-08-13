"""SSE 代理 + 出站 HTTP：payload 构造、SSE 解析、流式转发、report 处理。

依赖方向单向：依赖 file_state 与 file_store，不被它们反向调用。
注意：persisted_proxy_stream 不在此模块——它调用被测试 patch 的
insert_chat_message / proxy_stream，必须留在 main.py 才能让 patch 生效。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from auth import TOKEN_PATTERN
from config import settings
from file_state import MEDIA_PATTERN, inbox_file_records, inbox_lock
from file_store import extract_media_file_events, find_file_record_in_store
from models import ChatMessage
from progress import append_progress, summarize_progress_event


def build_responses_payload(
    messages: list[ChatMessage],
    conversation_id: str | None,
    stream: bool,
    model: str | None,
) -> dict[str, Any]:
    return {
        "model": model or settings.default_model,
        "input": [message.model_dump() for message in messages],
        "conversation": conversation_id,
        "store": True,
        "stream": stream,
    }


def target_responses_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/responses"


def sse_block(event: dict[str, Any]) -> bytes:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n".encode("utf-8")


def parse_sse_data(block: str) -> dict[str, Any] | None:
    data_lines = [
        line[5:].lstrip()
        for line in block.splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        return None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return None
    payload = json.loads(data)
    if not isinstance(payload, dict):
        return None
    return payload


def extract_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    pieces: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                pieces.append(content["text"])
    return "".join(pieces)


def extract_user_input_text(payload: dict[str, Any]) -> str:
    input_value = payload.get("input")
    if isinstance(input_value, str):
        return input_value
    if isinstance(input_value, list):
        pieces: list[str] = []
        for item in input_value:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    pieces.append(content)
            elif isinstance(item, str):
                pieces.append(item)
        return "\n".join(pieces)
    return ""


def prepare_agent_request(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    agent_payload = payload.copy()
    user_text = extract_user_input_text(payload)
    if "report_token" not in payload:
        return agent_payload, user_text, None

    report_token = payload["report_token"]
    if not isinstance(report_token, str) or not TOKEN_PATTERN.fullmatch(report_token):
        raise HTTPException(status_code=400, detail="Invalid report token")
    if not isinstance(payload.get("input"), str) or not user_text.strip():
        raise HTTPException(status_code=400, detail="Report processing requires a non-empty text input")

    report_path = resolve_inbox_report_path(report_token)
    agent_payload.pop("report_token")
    agent_payload["input"] = f"{user_text.rstrip()}\n\n文档路径为：{report_path}"
    return agent_payload, user_text, {"report_token": report_token}


def resolve_inbox_report_path(report_token: str) -> Path:
    with inbox_lock:
        record = inbox_file_records.get(report_token) or find_file_record_in_store(
            report_token,
            settings.inbox_file_store_dir,
            inbox_file_records,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Report file not found")

        resolved_store = settings.inbox_file_store_dir.resolve()
        resolved_report = record.path.resolve()
        if not resolved_report.is_relative_to(resolved_store):
            raise HTTPException(status_code=403, detail="Report file is outside the inbox store")
        if not resolved_report.is_file():
            raise HTTPException(status_code=404, detail="Report file not found")
        return resolved_report


async def proxy_stream(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    chat_collector: dict[str, Any] | None = None,
    progress_request_id: str | None = None,
    progress_client_id: str | None = None,
) -> AsyncIterator[bytes]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                body = await response.aread()
                body_text = body.decode("utf-8", errors="replace")
                error = {
                    "type": "proxy.error",
                    "status_code": response.status_code,
                    "body": body_text,
                }
                yield sse_block(error)
                yield sse_block(
                    {
                        "type": "response.output_text.delta",
                        "delta": f"Agent request failed with HTTP {response.status_code}: {body_text}",
                    }
                )
                return

            seen_media_paths: set[str] = set()
            async for output in transform_sse_stream(
                response.aiter_bytes(),
                seen_media_paths,
                chat_collector,
                progress_request_id,
                progress_client_id,
            ):
                yield output


async def transform_sse_stream(
    chunks: AsyncIterator[bytes],
    seen_media_paths: set[str],
    chat_collector: dict[str, Any] | None = None,
    progress_request_id: str | None = None,
    progress_client_id: str | None = None,
) -> AsyncIterator[bytes]:
    buffer = ""
    async for chunk in chunks:
        if not chunk:
            continue
        text = chunk.decode("utf-8", errors="replace")
        buffer += text

        separator_index = find_sse_separator(buffer)
        while separator_index != -1:
            block = buffer[:separator_index]
            separator_length = 4 if buffer[separator_index : separator_index + 4] == "\r\n\r\n" else 2
            buffer = buffer[separator_index + separator_length :]
            raw_block = f"{block}\n\n".encode("utf-8")
            yield raw_block

            event = parse_sse_data(block)
            if event is not None:
                collect_chat_delta(event, chat_collector)
                progress_message = summarize_progress_event(event)
                if progress_message:
                    append_progress(progress_client_id, progress_request_id, progress_message)
                for file_event in extract_media_file_events(event, seen_media_paths):
                    collect_chat_file(file_event, chat_collector)
                    yield sse_block(file_event)

            separator_index = find_sse_separator(buffer)

    if buffer:
        yield buffer.encode("utf-8")


def find_sse_separator(text: str) -> int:
    lf = text.find("\n\n")
    crlf = text.find("\r\n\r\n")
    if lf == -1:
        return crlf
    if crlf == -1:
        return lf
    return min(lf, crlf)


def collect_chat_delta(event: dict[str, Any], chat_collector: dict[str, Any] | None) -> None:
    if chat_collector is None:
        return
    text = assistant_delta_from_event(event)
    if text:
        chat_collector["text_parts"].append(text)


def collect_chat_file(file_event: dict[str, Any], chat_collector: dict[str, Any] | None) -> None:
    if chat_collector is None or file_event.get("type") != "proxy.file":
        return
    file = file_event.get("file")
    if isinstance(file, dict):
        chat_collector["files"].append(file)


def assistant_delta_from_event(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if isinstance(event_type, str):
        if not event_type.endswith(".delta"):
            return ""
        if event_type.startswith("response.function_call_arguments."):
            return ""

    for key in ("delta", "text", "output_text", "content"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return remove_media_markers(value)
    return ""


def remove_media_markers(text: str) -> str:
    cleaned = MEDIA_PATTERN.sub(lambda match: "\n" if match.group(0).startswith("\n") else "", text)
    return cleaned if cleaned.strip() else ""


async def request_json(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()
