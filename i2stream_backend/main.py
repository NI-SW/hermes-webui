from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from auth import (
    TOKEN_PATTERN,
    require_proxy_auth,
    validate_client_id,
    validate_request_id,
)
from chat_store import (
    assistant_message_exists,
    clear_visible_chat_messages,
    insert_chat_message,
    issue_chat_client_id,
    list_chat_messages,
    list_chat_messages_through,
    list_client_chat_conversation_summaries,
    list_client_chat_history,
)
from config import settings
from dashboard_agent import DashboardAgentError, DashboardAgentService
from dashboard_identity import DASHBOARD_USER_COOKIE_NAME, DashboardAnonymousIdentity
from dashboard_page import render_dashboard_page
from dashboard_session_store import SQLiteDashboardSessionStore
from datacop_client import DatacopClient
from dialoginteract import (
    DialogInteractionConflictError,
    DialogInteractionService,
    SQLiteFeedbackStore,
)
from file_state import file_records, inbox_file_records, inbox_lock
from file_store import (
    build_file_url,
    build_inbox_file_url,
    delete_inbox_record,
    extract_media_file_events,
    file_payload,
    file_response_from_store,
    find_file_record,
    find_file_record_in_store,
    list_report_files,
    resolve_inbox_upload,
    save_upload_file,
)
from gateway_bridge import (
    MAX_GATEWAY_FRAME_BYTES,
    GatewayApprovalConflictError,
    GatewayApprovalNotFoundError,
    GatewayBridge,
    GatewayClarificationConflictError,
    GatewayClarificationNotFoundError,
    GatewayProtocolError,
    GatewaySessionBusyError,
    GatewayUnavailableError,
    PendingGatewayRequest,
    derive_gateway_session_id,
    valid_gateway_approval_id,
    valid_gateway_authorization,
    valid_gateway_clarify_id,
)
from knowledge_store import (
    delete_vector_file,
    get_vector_task_status,
    list_vector_files,
    upload_knowledge_file,
)
from models import (
    ApprovalResponse,
    ClarificationResponse,
    CommunicateRequest,
    DashboardRunRequest,
    DashboardSessionCreateRequest,
    DashboardSessionUpdateRequest,
    HeartbeatRequest,
    MessageFeedbackRequest,
)
from node_store import OFFLINE_AFTER_SECONDS, delete_node, list_nodes, record_heartbeat
from progress import (
    append_progress,
    finish_progress,
    init_progress,
    progress_lock,
    progress_records,
    run_progress_cleanup,
)
from sse_proxy import (
    build_responses_payload,
    extract_output_text,
    prepare_agent_request,
    proxy_stream,
    remove_media_markers,
    request_json,
    sse_block,
    target_responses_url,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(
        run_progress_cleanup(),
        name="progress-ttl-cleanup",
    )
    await dashboard_agent_service.start()
    await dialog_interaction_service.start()
    try:
        yield
    finally:
        await dialog_interaction_service.stop()
        await dashboard_agent_service.stop()
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="Hermes Backend Bridge", version="0.1.0", lifespan=lifespan)
gateway_bridge = GatewayBridge(settings.request_timeout_seconds)
logger = logging.getLogger(__name__)
PUBLIC_GATEWAY_FAILURE_MESSAGE = "Agent request failed. Please retry."
MAX_SAFE_MESSAGE_ID = (1 << 53) - 1

datacop_client = DatacopClient(
    base_url=settings.datacop_base_url,
    project_id=settings.datacop_project_id,
    username=settings.datacop_username,
    password=settings.datacop_password,
    timeout_seconds=settings.datacop_timeout_seconds,
)
dialog_interaction_service = DialogInteractionService(
    bridge=gateway_bridge,
    session_secret=settings.session_hmac_secret,
    uploader=datacop_client,
    store=SQLiteFeedbackStore(),
    queue_size=settings.dialog_interaction_queue_size,
    job_ttl_seconds=settings.dialog_interaction_job_ttl_seconds,
)
dashboard_agent_service = DashboardAgentService(
    base_url=settings.dashboard_hermes_base_url,
    api_key=settings.dashboard_hermes_api_key,
    request_timeout_seconds=settings.request_timeout_seconds,
    session_store=SQLiteDashboardSessionStore(),
)
dashboard_anonymous_identity = DashboardAnonymousIdentity(settings.session_hmac_secret)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def dashboard_user_id(request: Request) -> str:
    user_id = getattr(request.state, "dashboard_user_id", None)
    if not isinstance(user_id, str):
        raise RuntimeError("Dashboard anonymous identity middleware did not run")
    return user_id


DashboardUserId = Annotated[str, Depends(dashboard_user_id)]


@app.exception_handler(DashboardAgentError)
async def dashboard_agent_error_handler(_: Request, exc: DashboardAgentError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.middleware("http")
async def dashboard_anonymous_identity_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    path = request.url.path
    if path != "/chat" and not path.startswith("/api/dashboard-agent/"):
        return await call_next(request)

    user_id, cookie_value = dashboard_anonymous_identity.resolve(
        request.cookies.get(DASHBOARD_USER_COOKIE_NAME),
    )
    request.state.dashboard_user_id = user_id
    response = await call_next(request)
    dashboard_anonymous_identity.set_cookie(
        response,
        cookie_value,
        secure=request.url.scheme == "https",
    )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class GatewayStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        pending: PendingGatewayRequest,
        bridge: GatewayBridge,
        progress_client_id: str,
        progress_request_id: str | None,
    ) -> None:
        self._pending_gateway_request = pending
        self._gateway_bridge = bridge
        self._progress_client_id = progress_client_id
        self._progress_request_id = progress_request_id
        self._cleanup_complete = False
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def cleanup(self) -> None:
        if self._cleanup_complete:
            return
        self._cleanup_complete = True
        finish_progress(self._progress_client_id, self._progress_request_id)
        await self._gateway_bridge.release_request(
            self._pending_gateway_request,
            notify_gateway=not self._pending_gateway_request.terminal_received,
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.cleanup()


async def persisted_proxy_stream(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    client_id: str,
    conversation_id: str,
    user_text: str,
    user_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    if user_text:
        insert_chat_message(client_id, conversation_id, "user", user_text, user_payload)

    collector: dict[str, Any] = {"text_parts": [], "files": []}
    try:
        async for chunk in proxy_stream(
            url,
            payload,
            api_key,
            collector,
            request_id,
            client_id,
        ):
            yield chunk
    finally:
        finish_progress(client_id, request_id)

    assistant_text = "".join(collector["text_parts"])
    files = collector["files"]
    if assistant_text.strip() or files:
        payload_json = {"files": files} if files else None
        insert_chat_message(client_id, conversation_id, "assistant", assistant_text, payload_json)


async def persisted_gateway_stream(
    pending: PendingGatewayRequest,
    client_id: str,
    conversation_id: str,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    files: list[dict[str, Any]] = []
    terminal_received = False
    try:
        yield sse_block({"type": "response.created"})
        async for event in gateway_bridge.iter_events(pending):
            event_type = event["type"]
            if event_type == "reasoning":
                progress_event = append_progress(client_id, request_id, event["text"])
                if progress_event is not None:
                    yield sse_block(
                        {
                            "type": "proxy.progress",
                            "request_id": request_id,
                            "seq": progress_event["seq"],
                            "message": progress_event["message"],
                        }
                    )
                continue
            if event_type == "tool.started":
                name = event["name"]
                progress_event = append_progress(
                    client_id,
                    request_id,
                    f"正在调用工具：{name}",
                )
                if progress_event is not None:
                    yield sse_block(
                        {
                            "type": "proxy.progress",
                            "request_id": request_id,
                            "seq": progress_event["seq"],
                            "message": progress_event["message"],
                        }
                    )
                yield sse_block(
                    {
                        "type": "response.output_item.added",
                        "item": {"type": "function_call", "name": name},
                    }
                )
                continue
            if event_type == "tool.completed":
                name = event["name"]
                progress_event = append_progress(
                    client_id,
                    request_id,
                    f"已完成调用工具：{name}",
                )
                if progress_event is not None:
                    yield sse_block(
                        {
                            "type": "proxy.progress",
                            "request_id": request_id,
                            "seq": progress_event["seq"],
                            "message": progress_event["message"],
                        }
                    )
                yield sse_block(
                    {
                        "type": "response.output_item.done",
                        "item": {"type": "function_call", "name": name},
                    }
                )
                continue
            if event_type == "clarify.request":
                yield sse_block(
                    {
                        "type": "response.clarification.requested",
                        "request_id": pending.request_id,
                        "clarification_id": event["clarify_id"],
                        "question": event["question"],
                        "choices": event["choices"],
                    }
                )
                continue
            if event_type == "approval.request":
                yield sse_block(
                    {
                        "type": "response.approval.requested",
                        "request_id": pending.request_id,
                        "approval_id": event["approval_id"],
                        "command": event["command"],
                        "description": event["description"],
                        "allow_permanent": event["allow_permanent"],
                        "allow_session": event["allow_session"],
                        "smart_denied": event["smart_denied"],
                    }
                )
                continue
            if event_type == "request.completed":
                final_text = event.get("text", "")
                clean_text = remove_media_markers(final_text)
                if clean_text:
                    yield sse_block({"type": "response.output_text.delta", "delta": clean_text})
                for file_event in extract_media_file_events(
                    {"type": "response.output_text.delta", "delta": final_text},
                    set(),
                ):
                    yield sse_block(file_event)
                    if file_event["type"] == "proxy.file":
                        files.append(file_event["file"])
                terminal_received = True
                if clean_text.strip() or files:
                    payload_json = {"files": files} if files else None
                    message_id = insert_chat_message(
                        client_id,
                        conversation_id,
                        "assistant",
                        clean_text,
                        payload_json,
                    )
                    yield sse_block(
                        {
                            "type": "proxy.message.persisted",
                            "role": "assistant",
                            "message_id": message_id,
                        }
                    )
                yield sse_block({"type": "response.completed"})
                yield b"data: [DONE]\n\n"
                continue
            if event_type == "request.failed":
                logger.error(
                    "Gateway request failed request_id=%s session_id=%s: %s",
                    pending.request_id,
                    pending.session_id,
                    event["error"],
                )
                terminal_received = True
                yield sse_block(
                    {
                        "type": "response.failed",
                        "error": {"message": PUBLIC_GATEWAY_FAILURE_MESSAGE},
                    }
                )
                yield b"data: [DONE]\n\n"
    except GatewayProtocolError as exc:
        logger.error(
            "Gateway protocol error request_id=%s session_id=%s: %s",
            pending.request_id,
            pending.session_id,
            exc,
        )
        yield sse_block(
            {
                "type": "response.failed",
                "error": {"message": PUBLIC_GATEWAY_FAILURE_MESSAGE},
            }
        )
        yield b"data: [DONE]\n\n"
    finally:
        finish_progress(client_id, request_id)
        await gateway_bridge.release_request(
            pending,
            notify_gateway=not terminal_received,
        )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "hermes_base_url": settings.hermes_base_url,
        "file_store_dir": str(settings.file_store_dir),
        "inbox_file_store_dir": str(settings.inbox_file_store_dir),
        "gateway_bridge_connected": gateway_bridge.connected,
    }


@app.post("/api/heartbeat", status_code=204)
async def heartbeat(payload: HeartbeatRequest) -> Response:
    record_heartbeat(payload.ip, utc_now())
    return Response(status_code=204)


@app.get("/api/nodes")
async def nodes() -> dict[str, Any]:
    return {
        "offline_after_seconds": OFFLINE_AFTER_SECONDS,
        "nodes": list_nodes(utc_now()),
    }


@app.delete("/api/nodes/{ip}", status_code=204)
async def remove_node(ip: str) -> Response:
    try:
        delete_node(ip)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return Response(status_code=204)


@app.websocket("/internal/gateway")
async def internal_gateway(websocket: WebSocket) -> None:
    if not valid_gateway_authorization(
        websocket.headers.get("authorization"),
        settings.gateway_bridge_token,
    ):
        await websocket.close(code=1008, reason="Invalid gateway authorization")
        return
    disconnect_reason = "Gateway bridge disconnected"
    await websocket.accept()
    if not await gateway_bridge.attach(websocket):
        await websocket.close(code=1008, reason="A gateway bridge is already connected")
        return
    try:
        while True:
            frame = await websocket.receive_json()
            await gateway_bridge.dispatch_event(frame)
    except WebSocketDisconnect:
        pass
    except (GatewayProtocolError, json.JSONDecodeError) as exc:
        disconnect_reason = f"Gateway protocol error: {exc}"
        try:
            await websocket.close(code=1003, reason="Invalid gateway protocol frame")
        except RuntimeError:
            pass
    finally:
        await gateway_bridge.detach(websocket, disconnect_reason)


@app.get("/", response_class=RedirectResponse)
async def root_page() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(render_dashboard_page("knowledge", vector_files=await list_vector_files()))


@app.post("/dashboard", response_class=HTMLResponse)
async def dashboard_upload_knowledge_page(
    file: Annotated[list[UploadFile], File()],
) -> HTMLResponse:
    uploaded_files = []
    for upload in file:
        uploaded_files.append(await upload_knowledge_file(upload))
    return HTMLResponse(
        render_dashboard_page(
            "knowledge",
            vector_files=await list_vector_files(),
            uploaded_knowledge_files=uploaded_files,
        )
    )


@app.post("/knowledge/files/{file_id}/delete", response_class=RedirectResponse)
async def dashboard_delete_knowledge_file(file_id: str) -> RedirectResponse:
    await delete_vector_file(file_id)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/knowledge/files/delete", response_class=RedirectResponse)
async def dashboard_delete_knowledge_files(
    file_id: Annotated[list[str], Form()],
) -> RedirectResponse:
    if not file_id:
        raise HTTPException(status_code=400, detail="Select at least one knowledge file")
    for selected_file_id in file_id:
        await delete_vector_file(selected_file_id)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/reports", response_class=HTMLResponse)
async def reports_page() -> HTMLResponse:
    return HTMLResponse(render_dashboard_page("reports", report_files=list_report_files()))


@app.get("/chat", response_class=HTMLResponse)
async def dashboard_chat_page() -> HTMLResponse:
    return HTMLResponse(render_dashboard_page("chat"))


@app.post("/reports/{token}/delete", response_class=RedirectResponse)
async def dashboard_delete_report(token: str) -> RedirectResponse:
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=400, detail="Invalid file token")
    with inbox_lock:
        delete_inbox_record(token)
    return RedirectResponse(url="/reports", status_code=303)


@app.get("/chat-history", response_class=HTMLResponse)
async def chat_history_page(client_id: str | None = None) -> HTMLResponse:
    if client_id is None or not client_id.strip():
        return HTMLResponse(render_dashboard_page("chat-history"))
    validated_client_id = validate_client_id(client_id.strip())
    conversations = list_client_chat_history(validated_client_id)
    return HTMLResponse(render_dashboard_page("chat-history", chat_client_id=validated_client_id, chat_conversations=conversations))


@app.get("/api/dashboard-agent/sessions")
async def api_list_dashboard_agent_sessions(owner_id: DashboardUserId) -> dict[str, Any]:
    sessions = await dashboard_agent_service.list_sessions(owner_id)
    return {"code": 0, "status": "success", "sessions": sessions}


@app.post("/api/dashboard-agent/sessions", status_code=201)
async def api_create_dashboard_agent_session(
    payload: DashboardSessionCreateRequest,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    session = await dashboard_agent_service.create_session(owner_id, payload.title)
    return {"code": 0, "status": "success", "session": session}


@app.patch("/api/dashboard-agent/sessions/{session_id}")
async def api_update_dashboard_agent_session(
    session_id: str,
    payload: DashboardSessionUpdateRequest,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    session = await dashboard_agent_service.update_session(owner_id, session_id, payload.title)
    return {"code": 0, "status": "success", "session": session}


@app.delete("/api/dashboard-agent/sessions/{session_id}")
async def api_delete_dashboard_agent_session(
    session_id: str,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    deleted = await dashboard_agent_service.delete_session(owner_id, session_id)
    return {"code": 0, "status": "success", "id": session_id, "deleted": deleted}


@app.get("/api/dashboard-agent/sessions/{session_id}/messages")
async def api_list_dashboard_agent_messages(
    session_id: str,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    messages = await dashboard_agent_service.list_messages(owner_id, session_id)
    return {
        "code": 0,
        "status": "success",
        "session_id": session_id,
        "messages": messages,
    }


@app.get("/api/dashboard-agent/sessions/{session_id}/active-run")
async def api_get_dashboard_agent_active_run(
    session_id: str,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    run = await dashboard_agent_service.get_active_run(owner_id, session_id)
    return {"code": 0, "status": "success", "run": run}


@app.post("/api/dashboard-agent/sessions/{session_id}/runs", status_code=202)
async def api_start_dashboard_agent_run(
    session_id: str,
    payload: DashboardRunRequest,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    run = await dashboard_agent_service.start_run(owner_id, session_id, payload.message)
    return {
        "code": 0,
        "status": "success",
        "run_id": run["run_id"],
        "run_status": run["status"],
    }


@app.get("/api/dashboard-agent/runs/{run_id}/events")
async def api_get_dashboard_agent_run_events(
    run_id: str,
    owner_id: DashboardUserId,
    after: int = 0,
) -> dict[str, Any]:
    if after < 0:
        raise DashboardAgentError(400, "after must be non-negative")
    snapshot = await dashboard_agent_service.get_run_events(owner_id, run_id, after=after)
    return {
        "code": 0,
        "status": "success",
        "run_id": snapshot["run_id"],
        "run_status": snapshot["status"],
        "done": snapshot["done"],
        "truncated": snapshot["truncated"],
        "events": snapshot["events"],
    }


@app.post("/api/dashboard-agent/runs/{run_id}/approval")
async def api_approve_dashboard_agent_run(
    run_id: str,
    payload: ApprovalResponse,
    owner_id: DashboardUserId,
) -> dict[str, Any]:
    response = await dashboard_agent_service.approve_run(owner_id, run_id, payload.choice)
    return {"code": 0, "status": "success", **response}


@app.post("/api/dashboard-agent/runs/{run_id}/stop")
async def api_stop_dashboard_agent_run(run_id: str, owner_id: DashboardUserId) -> dict[str, Any]:
    response = await dashboard_agent_service.stop_run(owner_id, run_id)
    return {
        "code": 0,
        "status": "success",
        "run_id": response["run_id"],
        "run_status": response["status"],
    }


@app.post("/api/knowledge/files")
async def api_upload_knowledge_file(
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    return {
        "code": 0,
        "status": "success",
        "file": await upload_knowledge_file(file),
    }


@app.get("/api/knowledge/files")
async def api_list_knowledge_files() -> dict[str, Any]:
    files = await list_vector_files()
    return {
        "code": 0,
        "status": "success",
        "files": files,
        "total": len(files),
    }


@app.delete("/api/knowledge/files/{file_id}")
async def api_delete_knowledge_file(file_id: str) -> dict[str, Any]:
    return {
        "code": 0,
        "status": "success",
        "file": await delete_vector_file(file_id),
    }


@app.get("/api/knowledge/tasks/{task_id}")
async def api_get_knowledge_task(task_id: str) -> dict[str, Any]:
    return {
        "code": 0,
        "status": "success",
        "task": await get_vector_task_status(task_id),
    }


@app.get("/api/conversations/{conversation_id}/messages")
async def chat_messages(
    conversation_id: str,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    client_id = validate_client_id(x_i2h_client_id)
    return {
        "code": 0,
        "status": "success",
        "conversation_id": conversation_id,
        "messages": list_chat_messages(client_id, conversation_id),
    }


@app.get("/api/chat-clients/{client_id}/conversations")
async def chat_history(client_id: str, _: None = Depends(require_proxy_auth)) -> dict[str, Any]:
    validated_client_id = validate_client_id(client_id)
    return {
        "code": 0,
        "status": "success",
        "client_id": validated_client_id,
        "conversations": list_client_chat_history(validated_client_id),
    }


@app.get("/api/chat-conversations")
async def chat_conversations(
    client_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[int | None, Query(ge=1, le=MAX_SAFE_MESSAGE_ID)] = None,
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    validated_client_id = validate_client_id(client_id)
    conversations, next_before = list_client_chat_conversation_summaries(
        validated_client_id,
        limit,
        before,
    )
    return {
        "code": 0,
        "status": "success",
        "conversations": conversations,
        "next_before": next_before,
    }


@app.post("/api/conversations/{conversation_id}/messages/{message_id}/feedback")
async def submit_message_feedback(
    conversation_id: str,
    message_id: int,
    payload: MessageFeedbackRequest,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    _: None = Depends(require_proxy_auth),
) -> dict[str, object]:
    if message_id <= 0 or message_id > MAX_SAFE_MESSAGE_ID:
        raise HTTPException(status_code=400, detail="Invalid message id")
    client_id = validate_client_id(x_i2h_client_id)
    if not assistant_message_exists(client_id, conversation_id, message_id):
        raise HTTPException(status_code=404, detail="Assistant message not found")
    messages = list_chat_messages_through(client_id, conversation_id, message_id)
    try:
        return await dialog_interaction_service.submit(
            client_id=client_id,
            conversation_id=conversation_id,
            message_id=message_id,
            feedback=payload.feedback,
            messages=messages,
        )
    except DialogInteractionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/dialog-interactions/{job_id}")
async def get_dialog_interaction_job(
    job_id: str,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    _: None = Depends(require_proxy_auth),
) -> dict[str, object]:
    client_id = validate_client_id(x_i2h_client_id)
    job = dialog_interaction_service.get_job(client_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Dialog interaction job not found")
    return job


@app.post("/api/chat-clients")
async def chat_client_id(_: None = Depends(require_proxy_auth)) -> dict[str, Any]:
    return {
        "code": 0,
        "status": "success",
        "client_id": issue_chat_client_id(),
    }


@app.delete("/api/conversations/{conversation_id}/messages")
async def clear_chat(
    conversation_id: str,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    client_id = validate_client_id(x_i2h_client_id)
    hidden_before = clear_visible_chat_messages(client_id, conversation_id)
    return {
        "code": 0,
        "status": "success",
        "conversation_id": conversation_id,
        "hidden_before_message_id": hidden_before,
    }


@app.get("/api/agent/requests/{request_id}/progress")
async def get_chat_progress(
    request_id: str,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    after: int = 0,
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    validated_request_id = validate_request_id(request_id)
    client_id = validate_client_id(x_i2h_client_id)
    with progress_lock:
        record = progress_records.get((client_id, validated_request_id))
        if record is None:
            return {
                "code": 0,
                "status": "missing",
                "request_id": validated_request_id,
                "done": True,
                "events": [],
            }
        events = [event for event in record["events"] if int(event["seq"]) > after]
        return {
            "code": 0,
            "status": "success",
            "request_id": validated_request_id,
            "done": bool(record["done"]),
            "events": events,
        }


@app.post("/api/agent/requests", response_model=None)
async def responses_proxy(request: Request, _: None = Depends(require_proxy_auth)) -> StreamingResponse:
    payload = await request.json()
    payload["stream"] = True
    conversation_id = payload.get("conversation")
    client_id_header = request.headers.get("X-I2H-Client-Id")
    if not isinstance(client_id_header, str) or not client_id_header:
        raise HTTPException(status_code=400, detail="X-I2H-Client-Id header is required")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise HTTPException(status_code=400, detail="conversation is required")
    client_id = validate_client_id(client_id_header)
    agent_payload, user_text, user_payload = prepare_agent_request(payload)
    agent_payload.pop("conversation", None)
    request_id_header = request.headers.get("X-I2H-Request-Id")
    request_id = validate_request_id(request_id_header) if isinstance(request_id_header, str) and request_id_header else None
    if user_text:
        insert_chat_message(client_id, conversation_id, "user", user_text, user_payload)
    if request_id:
        init_progress(client_id, request_id)
    session_id = derive_gateway_session_id(
        client_id,
        conversation_id,
        settings.session_hmac_secret,
    )
    try:
        pending = await gateway_bridge.start_request(
            session_id,
            agent_payload,
            owner_client_id=client_id,
        )
    except GatewaySessionBusyError as exc:
        finish_progress(client_id, request_id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GatewayUnavailableError as exc:
        finish_progress(client_id, request_id)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    stream = persisted_gateway_stream(
        pending,
        client_id,
        conversation_id,
        request_id,
    )
    try:
        return GatewayStreamingResponse(
            stream,
            pending=pending,
            bridge=gateway_bridge,
            progress_client_id=client_id,
            progress_request_id=request_id,
        )
    except BaseException:
        finish_progress(client_id, request_id)
        await gateway_bridge.release_request(pending, notify_gateway=True)
        raise


@app.post(
    "/api/agent/requests/{request_id}/clarifications/{clarify_id}",
)
async def respond_to_clarification(
    request_id: str,
    clarify_id: str,
    payload: ClarificationResponse,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    validated_request_id = validate_request_id(request_id)
    if not valid_gateway_clarify_id(clarify_id):
        raise HTTPException(status_code=400, detail="Invalid clarification id")
    validated_clarify_id = clarify_id
    client_id = validate_client_id(x_i2h_client_id)
    response = payload.response.strip()
    if not response:
        raise HTTPException(
            status_code=400,
            detail="Clarification response must not be empty",
        )
    try:
        await gateway_bridge.respond_clarify(
            validated_request_id,
            validated_clarify_id,
            client_id,
            response,
        )
    except GatewayClarificationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent request not found") from exc
    except GatewayClarificationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GatewayUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "code": 0,
        "status": "success",
        "request_id": validated_request_id,
        "clarification_id": validated_clarify_id,
    }


@app.post(
    "/api/agent/requests/{request_id}/approvals/{approval_id}",
)
async def respond_to_approval(
    request_id: str,
    approval_id: str,
    payload: ApprovalResponse,
    x_i2h_client_id: str = Header(alias="X-I2H-Client-Id"),
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    validated_request_id = validate_request_id(request_id)
    if not valid_gateway_approval_id(approval_id):
        raise HTTPException(status_code=400, detail="Invalid approval id")
    validated_approval_id = approval_id
    client_id = validate_client_id(x_i2h_client_id)
    try:
        await gateway_bridge.respond_approval(
            validated_request_id,
            validated_approval_id,
            client_id,
            payload.choice,
        )
    except GatewayApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent request not found") from exc
    except GatewayApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GatewayUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "code": 0,
        "status": "success",
        "request_id": validated_request_id,
        "approval_id": validated_approval_id,
    }


@app.post("/api/agent/local-requests", response_model=None)
async def communicate(payload: CommunicateRequest) -> Response:
    target_url = target_responses_url(f"http://127.0.0.1:{payload.agent_port}")
    request_payload = build_responses_payload(
        messages=payload.messages,
        conversation_id=payload.conversation_id,
        stream=payload.stream,
        model=payload.model,
    )

    if payload.stream:
        return StreamingResponse(
            proxy_stream(target_url, request_payload, payload.api_key),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    response_payload = await request_json(target_url, request_payload, payload.api_key)
    output_text = extract_output_text(response_payload)
    conversation_id = payload.conversation_id or response_payload.get("conversation") or response_payload.get("id")
    return JSONResponse(
        {
            "code": 0,
            "status": "success",
            "conversation_id": conversation_id,
            "output": [{"role": "assistant", "content": output_text}],
        }
    )


@app.post("/api/generated-files")
async def upload_file(
    file: Annotated[UploadFile, File()],
    _: None = Depends(require_proxy_auth),
) -> dict[str, Any]:
    record = await save_upload_file(file, settings.file_store_dir, file_records)
    return {
        "code": 0,
        "status": "success",
        "file": file_payload(record, build_file_url(record.token)),
    }


@app.post("/api/reports")
async def upload_inbox_file(request: Request, _: None = Depends(require_proxy_auth)) -> dict[str, Any]:
    record = await resolve_inbox_upload(request)
    return {
        "code": 0,
        "status": "success",
        "file": file_payload(record, build_inbox_file_url(record.token)),
    }


@app.get("/api/reports")
async def list_inbox_files(_: None = Depends(require_proxy_auth)) -> dict[str, Any]:
    return {
        "code": 0,
        "status": "success",
        "files": list_report_files(),
        "server_time": time.time(),
    }


@app.delete("/api/reports/{token}")
async def delete_inbox_file(token: str, _: None = Depends(require_proxy_auth)) -> dict[str, Any]:
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=400, detail="Invalid file token")

    with inbox_lock:
        deleted = delete_inbox_record(token)
    return {
        "code": 0,
        "status": "success",
        "token": token,
        "deleted": deleted,
    }


@app.get("/api/generated-files/{token}/content")
async def download_file(token: str, _: None = Depends(require_proxy_auth)) -> FileResponse:
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=400, detail="Invalid file token")

    record = file_records.get(token) or find_file_record(token)
    if record is None:
        raise HTTPException(status_code=404, detail="File token not found")

    return file_response_from_store(record, settings.file_store_dir)


@app.get("/api/reports/{token}/content")
async def download_inbox_file(token: str, _: None = Depends(require_proxy_auth)) -> FileResponse:
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=400, detail="Invalid file token")

    record = inbox_file_records.get(token) or find_file_record_in_store(
        token,
        settings.inbox_file_store_dir,
        inbox_file_records,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="File token not found")

    return file_response_from_store(record, settings.inbox_file_store_dir)


def main() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "50091"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        ws_max_size=MAX_GATEWAY_FRAME_BYTES,
    )


if __name__ == "__main__":
    main()
