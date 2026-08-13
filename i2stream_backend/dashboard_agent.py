"""Dashboard 对话使用的 Hermes 8641 Sessions/Runs 适配器。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from dashboard_session_store import DashboardSessionStore

TERMINAL_RUN_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
MAX_RUN_EVENTS = 400
MAX_RUN_EVENT_BYTES = 32 * 1024
MAX_RUN_TOTAL_BYTES = 512 * 1024
RUN_RETENTION_SECONDS = 15 * 60
RUN_CLEANUP_INTERVAL_SECONDS = 60
TRUNCATION_SUFFIX = "\n[内容过长，已截断]"


class DashboardAgentError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DashboardAgentConfigurationError(DashboardAgentError):
    def __init__(self) -> None:
        super().__init__(503, "Dashboard Hermes API key is not configured")


class DashboardAgentSessionBusyError(DashboardAgentError):
    def __init__(self) -> None:
        super().__init__(409, "当前会话已有正在执行的请求")


class DashboardAgentUpstreamError(DashboardAgentError):
    pass


@dataclass
class DashboardRunRecord:
    run_id: str
    session_id: str
    owner_id: str
    status: str = "started"
    next_seq: int = 1
    events: list[dict[str, Any]] = field(default_factory=list)
    event_bytes: int = 0
    done: bool = False
    finished_at_monotonic: float | None = None
    task: asyncio.Task[None] | None = None


class DashboardAgentService:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        request_timeout_seconds: float,
        session_store: DashboardSessionStore,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._request_timeout_seconds = request_timeout_seconds
        self._session_store = session_store
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._runs: dict[str, DashboardRunRecord] = {}
        self._active_run_by_session: dict[str, str] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url)
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="dashboard-agent-run-cleanup",
            )

    async def stop(self) -> None:
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

        async with self._lock:
            run_tasks = [
                record.task
                for record in self._runs.values()
                if record.task is not None and not record.task.done()
            ]
        for task in run_tasks:
            task.cancel()
        if run_tasks:
            await asyncio.gather(*run_tasks, return_exceptions=True)

        client = self._client
        self._client = None
        if client is not None and self._owns_client:
            await client.aclose()

    async def list_sessions(self, owner_id: str) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for session_id in self._session_store.list_session_ids(owner_id):
            try:
                sessions.append(await self._get_session(session_id))
            except DashboardAgentUpstreamError as exc:
                if exc.status_code != 404:
                    raise
                self._session_store.remove(owner_id, session_id)
        return sessions

    async def create_session(self, owner_id: str, title: str) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            "/api/sessions",
            json_body={"title": title},
        )
        session = self._required_object(payload, "session", "created session")
        session_id = self._required_string(session, "id", "created session ID")
        self._session_store.add(owner_id, session_id)
        return session

    async def update_session(self, owner_id: str, session_id: str, title: str) -> dict[str, Any]:
        self._require_session_owner(owner_id, session_id)
        payload = await self._request_json(
            "PATCH",
            self._session_path(session_id),
            json_body={"title": title},
        )
        return self._required_object(payload, "session", "updated session")

    async def delete_session(self, owner_id: str, session_id: str) -> bool:
        self._require_session_owner(owner_id, session_id)
        async with self._lock:
            if session_id in self._active_run_by_session:
                raise DashboardAgentSessionBusyError()
        payload = await self._request_json("DELETE", self._session_path(session_id))
        deleted = payload.get("deleted")
        if not isinstance(deleted, bool):
            raise DashboardAgentUpstreamError(502, "Hermes returned an invalid delete response")
        if deleted and not self._session_store.remove(owner_id, session_id):
            raise RuntimeError("Dashboard session ownership disappeared during deletion")
        return deleted

    async def list_messages(self, owner_id: str, session_id: str) -> list[dict[str, Any]]:
        self._require_session_owner(owner_id, session_id)
        payload = await self._request_json(
            "GET",
            f"{self._session_path(session_id)}/messages",
        )
        return self._object_list(payload, "message list")

    async def start_run(self, owner_id: str, session_id: str, message: str) -> dict[str, str]:
        self._require_session_owner(owner_id, session_id)
        async with self._lock:
            if session_id in self._active_run_by_session:
                raise DashboardAgentSessionBusyError()
            self._active_run_by_session[session_id] = "starting"

        started = False
        try:
            messages = await self.list_messages(owner_id, session_id)
            history = self._conversation_history(messages)
            payload = await self._request_json(
                "POST",
                "/v1/runs",
                json_body={
                    "input": message,
                    "session_id": session_id,
                    "conversation_history": history,
                },
            )
            run_id = payload.get("run_id")
            status = payload.get("status")
            if not isinstance(run_id, str) or not run_id:
                raise DashboardAgentUpstreamError(502, "Hermes did not return a run ID")
            if not isinstance(status, str) or not status:
                raise DashboardAgentUpstreamError(502, "Hermes did not return a run status")

            record = DashboardRunRecord(
                run_id=run_id,
                session_id=session_id,
                owner_id=owner_id,
                status=status,
            )
            async with self._lock:
                if run_id in self._runs:
                    raise DashboardAgentUpstreamError(502, "Hermes returned a duplicate run ID")
                self._runs[run_id] = record
                self._active_run_by_session[session_id] = run_id
                record.task = asyncio.create_task(
                    self._pump_run_events(record),
                    name=f"dashboard-agent-run-{run_id}",
                )
                started = True
            return {"run_id": run_id, "status": status}
        finally:
            if not started:
                async with self._lock:
                    if self._active_run_by_session.get(session_id) == "starting":
                        self._active_run_by_session.pop(session_id, None)

    async def get_run_events(self, owner_id: str, run_id: str, *, after: int) -> dict[str, Any]:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.owner_id != owner_id:
                raise DashboardAgentError(404, "Dashboard Agent run not found")
            first_seq = record.events[0]["seq"] if record.events else record.next_seq
            return {
                "run_id": run_id,
                "status": record.status,
                "done": record.done,
                "truncated": after < first_seq - 1,
                "events": [event.copy() for event in record.events if event["seq"] > after],
            }

    async def get_active_run(self, owner_id: str, session_id: str) -> dict[str, str] | None:
        self._require_session_owner(owner_id, session_id)
        async with self._lock:
            run_id = self._active_run_by_session.get(session_id)
            if run_id is None or run_id == "starting":
                return None
            record = self._runs.get(run_id)
            if record is None or record.done:
                return None
            return {"run_id": run_id, "status": record.status}

    async def approve_run(
        self,
        owner_id: str,
        run_id: str,
        choice: Literal["once", "session", "always", "deny"],
    ) -> dict[str, Any]:
        await self._require_run_owner(owner_id, run_id)
        payload = await self._request_json(
            "POST",
            f"/v1/runs/{quote(run_id, safe='')}/approval",
            json_body={"choice": choice},
        )
        resolved = payload.get("resolved")
        response_run_id = payload.get("run_id")
        response_choice = payload.get("choice")
        if not isinstance(resolved, int) or resolved <= 0:
            raise DashboardAgentUpstreamError(502, "Hermes did not resolve the approval")
        if response_run_id != run_id or response_choice != choice:
            raise DashboardAgentUpstreamError(502, "Hermes returned a mismatched approval response")
        return {"run_id": run_id, "choice": choice, "resolved": resolved}

    async def stop_run(self, owner_id: str, run_id: str) -> dict[str, str]:
        await self._require_run_owner(owner_id, run_id)
        payload = await self._request_json(
            "POST",
            f"/v1/runs/{quote(run_id, safe='')}/stop",
            json_body={},
        )
        response_run_id = payload.get("run_id")
        status = payload.get("status")
        if response_run_id != run_id or not isinstance(status, str):
            raise DashboardAgentUpstreamError(502, "Hermes returned an invalid stop response")
        async with self._lock:
            record = self._runs.get(run_id)
            if record is not None and not record.done:
                record.status = status
        return {"run_id": run_id, "status": status}

    async def _pump_run_events(self, record: DashboardRunRecord) -> None:
        terminal_seen = False
        try:
            client = self._required_client()
            self._require_api_key()
            timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            async with client.stream(
                "GET",
                f"/v1/runs/{quote(record.run_id, safe='')}/events",
                headers=self._headers(),
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise self._upstream_error(response.status_code, body)
                async for event in self._iter_sse_events(response):
                    event_type = event.get("event")
                    if not isinstance(event_type, str) or not event_type:
                        raise ValueError("Hermes run event is missing its event type")
                    await self._append_event(record, event)
                    terminal_seen = event_type in TERMINAL_RUN_EVENTS
                    if terminal_seen:
                        break
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, DashboardAgentError, ValueError, json.JSONDecodeError) as exc:
            await self._append_event(
                record,
                {
                    "event": "run.failed",
                    "run_id": record.run_id,
                    "error": f"Hermes event stream failed: {exc}",
                },
            )
            terminal_seen = True
        finally:
            if not terminal_seen:
                await self._append_event(
                    record,
                    {
                        "event": "run.failed",
                        "run_id": record.run_id,
                        "error": "Hermes event stream ended before a terminal event",
                    },
                )
            await self._finish_record(record)

    async def _append_event(self, record: DashboardRunRecord, event: dict[str, Any]) -> None:
        bounded = self._bound_event(event)
        async with self._lock:
            current = self._runs.get(record.run_id)
            if current is not record or record.done:
                return
            bounded["seq"] = record.next_seq
            record.next_seq += 1
            encoded_size = len(json.dumps(bounded, ensure_ascii=False).encode("utf-8"))
            record.events.append(bounded)
            record.event_bytes += encoded_size
            event_type = bounded["event"]
            if event_type == "approval.request":
                record.status = "waiting_for_approval"
            elif event_type == "approval.responded":
                record.status = "running"
            elif event_type in TERMINAL_RUN_EVENTS:
                record.status = event_type.removeprefix("run.")
            elif record.status == "started":
                record.status = "running"
            while len(record.events) > MAX_RUN_EVENTS or record.event_bytes > MAX_RUN_TOTAL_BYTES:
                removed = record.events.pop(0)
                record.event_bytes -= len(json.dumps(removed, ensure_ascii=False).encode("utf-8"))

    async def _finish_record(self, record: DashboardRunRecord) -> None:
        async with self._lock:
            current = self._runs.get(record.run_id)
            if current is not record:
                return
            record.done = True
            if record.status not in TERMINAL_RUN_STATUSES:
                record.status = "failed"
            record.finished_at_monotonic = time.monotonic()
            if self._active_run_by_session.get(record.session_id) == record.run_id:
                self._active_run_by_session.pop(record.session_id, None)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(RUN_CLEANUP_INTERVAL_SECONDS)
            now = time.monotonic()
            async with self._lock:
                expired = [
                    run_id
                    for run_id, record in self._runs.items()
                    if record.done
                    and record.finished_at_monotonic is not None
                    and now - record.finished_at_monotonic >= RUN_RETENTION_SECONDS
                ]
                for run_id in expired:
                    del self._runs[run_id]

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._required_client()
        self._require_api_key()
        try:
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self._request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DashboardAgentUpstreamError(502, f"Unable to reach Hermes 8641: {exc}") from exc
        if response.status_code >= 400:
            raise self._upstream_error(response.status_code, response.content)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise DashboardAgentUpstreamError(502, "Hermes returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DashboardAgentUpstreamError(502, "Hermes returned a non-object JSON response")
        return payload

    async def _iter_sse_events(self, response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    yield self._decode_sse_data(data_lines)
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield self._decode_sse_data(data_lines)

    @staticmethod
    def _decode_sse_data(data_lines: list[str]) -> dict[str, Any]:
        payload = json.loads("\n".join(data_lines))
        if not isinstance(payload, dict):
            raise ValueError("Hermes SSE data must be a JSON object")
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Accept": "application/json, text/event-stream",
        }

    def _require_api_key(self) -> None:
        if not self._api_key.get_secret_value():
            raise DashboardAgentConfigurationError()

    def _required_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("DashboardAgentService has not been started")
        return self._client

    async def _get_session(self, session_id: str) -> dict[str, Any]:
        payload = await self._request_json("GET", self._session_path(session_id))
        return self._required_object(payload, "session", "session")

    def _require_session_owner(self, owner_id: str, session_id: str) -> None:
        if not self._session_store.owns(owner_id, session_id):
            raise DashboardAgentError(404, "Dashboard Agent session not found")

    async def _require_run_owner(self, owner_id: str, run_id: str) -> DashboardRunRecord:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.owner_id != owner_id:
                raise DashboardAgentError(404, "Dashboard Agent run not found")
            return record

    @staticmethod
    def _session_path(session_id: str) -> str:
        if not session_id:
            raise DashboardAgentError(400, "session_id must not be empty")
        return f"/api/sessions/{quote(session_id, safe='')}"

    @staticmethod
    def _required_object(payload: dict[str, Any], key: str, label: str) -> dict[str, Any]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise DashboardAgentUpstreamError(502, f"Hermes returned an invalid {label}")
        return value

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str, label: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise DashboardAgentUpstreamError(502, f"Hermes returned an invalid {label}")
        return value

    @staticmethod
    def _object_list(payload: dict[str, Any], label: str) -> list[dict[str, Any]]:
        value = payload.get("data")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise DashboardAgentUpstreamError(502, f"Hermes returned an invalid {label}")
        return value

    @staticmethod
    def _conversation_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            history.append({"role": role, "content": content})
        return history

    @staticmethod
    def _upstream_error(status_code: int, body: bytes) -> DashboardAgentUpstreamError:
        detail = body.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = error["message"]
        public_status = status_code if 400 <= status_code < 500 else 502
        return DashboardAgentUpstreamError(public_status, f"Hermes 8641 rejected the request: {detail}")

    @staticmethod
    def _bound_event(event: dict[str, Any]) -> dict[str, Any]:
        bounded = event.copy()
        truncated_fields: list[str] = []
        for field_name in ("text", "delta", "output", "command", "description", "error", "preview"):
            value = bounded.get(field_name)
            if not isinstance(value, str):
                continue
            encoded = value.encode("utf-8")
            if len(encoded) <= MAX_RUN_EVENT_BYTES:
                continue
            suffix = TRUNCATION_SUFFIX.encode("utf-8")
            prefix = encoded[: MAX_RUN_EVENT_BYTES - len(suffix)].decode("utf-8", errors="ignore")
            bounded[field_name] = f"{prefix}{TRUNCATION_SUFFIX}"
            truncated_fields.append(field_name)
        if truncated_fields:
            bounded["truncated_fields"] = truncated_fields
        return bounded
