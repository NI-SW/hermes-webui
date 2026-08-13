from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any

progress_lock = Lock()
progress_records: dict[tuple[str, str], dict[str, Any]] = {}
PROGRESS_RETENTION_SECONDS = 15 * 60
PROGRESS_CLEANUP_INTERVAL_SECONDS = 60
MAX_PROGRESS_EVENT_BYTES = 16 * 1024
MAX_PROGRESS_TOTAL_BYTES = 256 * 1024
MAX_PROGRESS_EVENTS = 200
PROGRESS_TRUNCATION_SUFFIX = "\n[进度内容过长，已截断]"


def init_progress(client_id: str, request_id: str) -> None:
    with progress_lock:
        progress_records[(client_id, request_id)] = {
            "next_seq": 1,
            "events": [],
            "event_bytes": 0,
            "done": False,
            "updated_at": time.time(),
            "finished_at_monotonic": None,
        }
        append_progress_locked(client_id, request_id, "Agent 已接收请求，正在准备执行。")


def append_progress_locked(
    client_id: str,
    request_id: str,
    message: str,
) -> dict[str, Any] | None:
    record = progress_records.get((client_id, request_id))
    if record is None:
        return None
    message, message_bytes = bound_progress_message(message)
    events = record["events"]
    if events and events[-1]["message"] == message:
        return None
    event = {
        "seq": record["next_seq"],
        "message": message,
        "created_at": time.time(),
    }
    record["next_seq"] += 1
    events.append(event)
    record["event_bytes"] += message_bytes
    while (
        len(events) > MAX_PROGRESS_EVENTS
        or record["event_bytes"] > MAX_PROGRESS_TOTAL_BYTES
    ):
        removed = events.pop(0)
        record["event_bytes"] -= len(removed["message"].encode("utf-8"))
    record["updated_at"] = time.time()
    return event


def bound_progress_message(message: str) -> tuple[str, int]:
    encoded = message.encode("utf-8")
    if len(encoded) <= MAX_PROGRESS_EVENT_BYTES:
        return message, len(encoded)

    suffix = PROGRESS_TRUNCATION_SUFFIX.encode("utf-8")
    prefix_bytes = encoded[: MAX_PROGRESS_EVENT_BYTES - len(suffix)]
    prefix = prefix_bytes.decode("utf-8", errors="ignore")
    bounded = f"{prefix}{PROGRESS_TRUNCATION_SUFFIX}"
    return bounded, len(bounded.encode("utf-8"))


def append_progress(
    client_id: str | None,
    request_id: str | None,
    message: str,
) -> dict[str, Any] | None:
    if not client_id or not request_id:
        return None
    with progress_lock:
        return append_progress_locked(client_id, request_id, message)


def finish_progress(client_id: str | None, request_id: str | None) -> None:
    if not client_id or not request_id:
        return
    with progress_lock:
        record = progress_records.get((client_id, request_id))
        if record is None:
            return
        if record["done"]:
            return
        record["done"] = True
        record["updated_at"] = time.time()
        record["finished_at_monotonic"] = time.monotonic()


def cleanup_expired_progress(*, now_monotonic: float | None = None) -> int:
    current_time = time.monotonic() if now_monotonic is None else now_monotonic
    with progress_lock:
        expired_keys = [
            key
            for key, record in progress_records.items()
            if record["done"]
            and current_time - record["finished_at_monotonic"]
            >= PROGRESS_RETENTION_SECONDS
        ]
        for key in expired_keys:
            del progress_records[key]
    return len(expired_keys)


async def run_progress_cleanup() -> None:
    while True:
        await asyncio.sleep(PROGRESS_CLEANUP_INTERVAL_SECONDS)
        cleanup_expired_progress()


def summarize_progress_event(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    item = event.get("item")
    if not isinstance(event_type, str) or not isinstance(item, dict):
        return ""

    item_type = item.get("type")
    if event_type == "response.output_item.added" and item_type == "function_call":
        return summarize_function_call_progress(item, "正在")
    if event_type == "response.output_item.done" and item_type == "function_call":
        return summarize_function_call_progress(item, "已完成")
    if item_type == "function_call_output":
        return "当前工具已返回结果，正在整理回复。"
    return ""


def summarize_function_call_progress(item: dict[str, Any], verb: str) -> str:
    name = item.get("name")
    arguments = parse_json_object(item.get("arguments"))
    if name == "skill_view":
        skill_name = arguments.get("name")
        return f"{verb}加载 skill：{skill_name}" if isinstance(skill_name, str) and skill_name else f"{verb}加载 skill。"
    if name == "terminal":
        command = arguments.get("command")
        return f"{verb}执行命令：{summarize_command(command)}"
    if isinstance(name, str) and name:
        return f"{verb}调用工具：{name}"
    return f"{verb}调用工具。"


def parse_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def summarize_command(command: Any) -> str:
    if not isinstance(command, str) or not command:
        return "terminal"
    script_match = re.search(r"(?:^|\s)([\w/-]+\.py)(?:\s|$)", command)
    if not script_match:
        return "terminal"

    script_name = Path(script_match.group(1)).name
    flags: list[str] = []
    for flag in ("--list", "--rule-name", "--info", "--search", "--search-field", "--mode"):
        value_match = re.search(rf"{re.escape(flag)}(?:\s+([^\s]+))?", command)
        if not value_match:
            continue
        value = value_match.group(1)
        flags.append(f"{flag} {value}" if value else flag)
    return f"{script_name} {' '.join(flags)}" if flags else script_name
