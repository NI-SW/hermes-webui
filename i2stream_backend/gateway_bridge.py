"""Agent Console 与 Hermes Gateway 平台插件之间的内存态 WebSocket Bridge。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import WebSocketDisconnect
from pydantic import SecretStr


TERMINAL_EVENT_TYPES = frozenset({"request.completed", "request.failed"})
GATEWAY_EVENT_TYPES = frozenset(
    {
        "assistant.snapshot",
        "approval.request",
        "approval.response",
        "clarify.request",
        "clarify.response",
        "reasoning",
        "tool.started",
        "tool.completed",
        "request.completed",
        "request.failed",
    }
)
GATEWAY_EVENT_QUEUE_SIZE = 256
MAX_GATEWAY_FRAME_BYTES = 2 * 1024 * 1024
MAX_REASONING_TEXT_BYTES = 64 * 1024
MAX_ASSISTANT_TEXT_BYTES = 256 * 1024
MAX_CLARIFY_QUESTION_LENGTH = 10_000
MAX_CLARIFY_CHOICES = 20
MAX_CLARIFY_CHOICE_LENGTH = 2_000
CLARIFY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_APPROVAL_COMMAND_LENGTH = 10_000
MAX_APPROVAL_DESCRIPTION_LENGTH = 10_000
APPROVAL_CHOICES = frozenset({"once", "session", "always", "deny"})
APPROVAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DEFAULT_CLARIFICATION_TIMEOUT_SECONDS = 3600.0
DEFAULT_CLARIFICATION_ACK_TIMEOUT_SECONDS = 10.0
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
DEFAULT_APPROVAL_ACK_TIMEOUT_SECONDS = 10.0


class GatewaySocket(Protocol):
    async def send_json(self, payload: dict[str, Any]) -> None: ...


class GatewayBridgeError(RuntimeError):
    """Bridge 请求错误基类。"""


class GatewayUnavailableError(GatewayBridgeError):
    """Hermes Gateway 插件尚未连接或连接已失效。"""


class GatewaySessionBusyError(GatewayBridgeError):
    """同一内部会话已有请求执行中。"""


class GatewayProtocolError(GatewayBridgeError):
    """Gateway 发来的帧不符合协议。"""


class GatewayClarificationNotFoundError(GatewayBridgeError):
    """请求不存在，或不属于提交回答的客户端。"""


class GatewayClarificationConflictError(GatewayBridgeError):
    """Clarify 请求已失效、已回答或 ID 不匹配。"""


class GatewayApprovalNotFoundError(GatewayBridgeError):
    """请求不存在，或不属于提交授权选择的客户端。"""


class GatewayApprovalConflictError(GatewayBridgeError):
    """Approval 请求已失效、已回答或不是 FIFO 队首。"""


@dataclass(frozen=True, slots=True)
class PendingClarification:
    clarify_id: str
    question: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClarificationResponseOutcome:
    accepted: bool
    error: GatewayBridgeError | None = None


@dataclass(frozen=True, slots=True)
class PendingApproval:
    approval_id: str
    command: str
    description: str
    allow_permanent: bool
    allow_session: bool
    smart_denied: bool

    def allows(self, choice: str) -> bool:
        if choice in {"once", "deny"}:
            return True
        if self.smart_denied:
            return False
        if choice == "session":
            return self.allow_session
        if choice == "always":
            return self.allow_permanent
        return False


@dataclass(frozen=True, slots=True)
class ApprovalResponseOutcome:
    accepted: bool
    error: GatewayBridgeError | None = None


@dataclass(slots=True)
class PendingGatewayRequest:
    request_id: str
    session_id: str
    owner_client_id: str
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=GATEWAY_EVENT_QUEUE_SIZE)
    )
    terminal_received: bool = False
    accepting_events: bool = True
    latest_snapshot: str = ""
    current_clarification: PendingClarification | None = None
    last_settled_clarify_id: str | None = None
    clarification_response_waiter: (
        asyncio.Future[ClarificationResponseOutcome] | None
    ) = None
    clarification_state_changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    settled_approval_ids: set[str] = field(default_factory=set)
    approval_response_waiter: asyncio.Future[ApprovalResponseOutcome] | None = None
    approval_state_changed: asyncio.Event = field(default_factory=asyncio.Event)


def derive_gateway_session_id(
    client_id: str,
    conversation_id: str,
    secret: SecretStr,
) -> str:
    canonical = json.dumps(
        ["v1", client_id, conversation_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        secret.get_secret_value().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def valid_gateway_authorization(authorization: str | None, token: SecretStr) -> bool:
    if not isinstance(authorization, str):
        return False
    scheme, separator, supplied_token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied_token:
        return False
    return hmac.compare_digest(supplied_token, token.get_secret_value())


def valid_gateway_clarify_id(clarify_id: str) -> bool:
    return bool(CLARIFY_ID_PATTERN.fullmatch(clarify_id))


def valid_gateway_approval_id(approval_id: str) -> bool:
    return bool(APPROVAL_ID_PATTERN.fullmatch(approval_id))


class GatewayBridge:
    def __init__(
        self,
        request_timeout_seconds: float,
        clarification_timeout_seconds: float = DEFAULT_CLARIFICATION_TIMEOUT_SECONDS,
        clarification_ack_timeout_seconds: float = DEFAULT_CLARIFICATION_ACK_TIMEOUT_SECONDS,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        approval_ack_timeout_seconds: float = DEFAULT_APPROVAL_ACK_TIMEOUT_SECONDS,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if clarification_timeout_seconds <= 0:
            raise ValueError("clarification_timeout_seconds must be positive")
        if clarification_ack_timeout_seconds <= 0:
            raise ValueError("clarification_ack_timeout_seconds must be positive")
        if approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds must be positive")
        if approval_ack_timeout_seconds <= 0:
            raise ValueError("approval_ack_timeout_seconds must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        self._clarification_timeout_seconds = clarification_timeout_seconds
        self._clarification_ack_timeout_seconds = clarification_ack_timeout_seconds
        self._approval_timeout_seconds = approval_timeout_seconds
        self._approval_ack_timeout_seconds = approval_ack_timeout_seconds
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._websocket: GatewaySocket | None = None
        self._pending_by_request: dict[str, PendingGatewayRequest] = {}
        self._request_by_session: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    async def attach(self, websocket: GatewaySocket) -> bool:
        async with self._state_lock:
            if self._websocket is not None:
                return False
            self._websocket = websocket
            return True

    async def detach(self, websocket: GatewaySocket, reason: str) -> None:
        async with self._state_lock:
            if self._websocket is not websocket:
                return
            self._websocket = None
            pending_requests = list(self._pending_by_request.values())
            self._pending_by_request.clear()
            self._request_by_session.clear()
            for pending in pending_requests:
                if pending.terminal_received:
                    continue
                pending.terminal_received = True
                pending.accepting_events = False
                self._finish_clarification(
                    pending,
                    error=GatewayUnavailableError(reason),
                )
                self._abort_approvals(
                    pending,
                    error=GatewayUnavailableError(reason),
                )
                self._replace_queue_with_event(
                    pending,
                    {
                        "type": "request.failed",
                        "request_id": pending.request_id,
                        "session_id": pending.session_id,
                        "error": reason,
                    },
                )

    async def start_request(
        self,
        session_id: str,
        payload: dict[str, Any],
        owner_client_id: str,
    ) -> PendingGatewayRequest:
        pending = PendingGatewayRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            owner_client_id=owner_client_id,
        )
        async with self._state_lock:
            websocket = self._websocket
            if websocket is None:
                raise GatewayUnavailableError("Hermes Gateway bridge is not connected")
            if session_id in self._request_by_session:
                raise GatewaySessionBusyError("A request is already running for this conversation")
            self._pending_by_request[pending.request_id] = pending
            self._request_by_session[session_id] = pending.request_id

        frame = {
            "type": "request.start",
            "request_id": pending.request_id,
            "session_id": session_id,
            "payload": payload,
        }
        send_started = False
        try:
            async with self._send_lock:
                send_started = True
                await websocket.send_json(frame)
        except asyncio.CancelledError:
            rollback_task = asyncio.create_task(
                self._rollback_start(pending, websocket, send_started)
            )
            try:
                await asyncio.shield(rollback_task)
            except asyncio.CancelledError:
                await rollback_task
            raise
        except (OSError, RuntimeError, WebSocketDisconnect) as exc:
            await self.detach(websocket, "Gateway bridge disconnected while starting request")
            raise GatewayUnavailableError(
                "Hermes Gateway bridge disconnected while starting request"
            ) from exc
        return pending

    async def dispatch_event(self, frame: object) -> None:
        event = self._validate_event(frame)
        request_id = event["request_id"]

        async with self._state_lock:
            pending = self._pending_by_request.get(request_id)
            if pending is None:
                # request.cancel 与远端终态可能交错；迟到事件不再有接收方。
                return
            if event["session_id"] != pending.session_id:
                raise GatewayProtocolError("Gateway event session_id does not match request")
            if pending.terminal_received:
                raise GatewayProtocolError("Gateway sent an event after request terminal state")
            if not pending.accepting_events:
                return

            event_type = event["type"]
            if event_type == "approval.response":
                approval_id = event["approval_id"]
                current_approval = (
                    pending.pending_approvals[0]
                    if pending.pending_approvals
                    else None
                )
                if (
                    approval_id in pending.settled_approval_ids
                    and (
                        current_approval is None
                        or current_approval.approval_id != approval_id
                    )
                ):
                    # 超时、取消或终态可能与已在途的回执交错。
                    return
                waiter = pending.approval_response_waiter
                if (
                    current_approval is None
                    or waiter is None
                    or current_approval.approval_id != approval_id
                ):
                    raise GatewayProtocolError(
                        "Gateway approval.response does not match an active response"
                    )
                error = None
                if not event["accepted"]:
                    error = GatewayApprovalConflictError(
                        "Hermes Gateway rejected approval response"
                    )
                self._finish_current_approval(
                    pending,
                    accepted=event["accepted"],
                    error=error,
                )
                return
            if event_type == "clarify.response":
                clarification = pending.current_clarification
                if (
                    pending.last_settled_clarify_id == event["clarify_id"]
                    and (
                        clarification is None
                        or clarification.clarify_id != event["clarify_id"]
                    )
                ):
                    # 超时、取消或终态可能与已在途的回执交错。
                    return
                waiter = pending.clarification_response_waiter
                if (
                    clarification is None
                    or waiter is None
                    or clarification.clarify_id != event["clarify_id"]
                ):
                    raise GatewayProtocolError(
                        "Gateway clarify.response does not match an active response"
                    )
                error = None
                if not event["accepted"]:
                    error = GatewayClarificationConflictError(
                        "Hermes Gateway rejected clarification response"
                    )
                self._finish_clarification(
                    pending,
                    accepted=event["accepted"],
                    error=error,
                )
                return
            if event_type == "assistant.snapshot":
                pending.latest_snapshot = event["text"]
                return
            if event_type == "request.completed" and "text" not in event:
                event = {**event, "text": pending.latest_snapshot}
            if event_type == "approval.request":
                approval_id = event["approval_id"]
                if (
                    approval_id in pending.settled_approval_ids
                    or any(
                        approval.approval_id == approval_id
                        for approval in pending.pending_approvals
                    )
                ):
                    raise GatewayProtocolError(
                        "Gateway sent a duplicate approval.request approval_id"
                    )
                if not pending.pending_approvals:
                    pending.approval_state_changed.clear()
                pending.pending_approvals.append(
                    PendingApproval(
                        approval_id=approval_id,
                        command=event["command"],
                        description=event["description"],
                        allow_permanent=event["allow_permanent"],
                        allow_session=event["allow_session"],
                        smart_denied=event["smart_denied"],
                    )
                )
            if event_type == "clarify.request":
                if pending.current_clarification is not None:
                    raise GatewayProtocolError(
                        "Gateway sent clarify.request while another clarification is active"
                    )
                pending.current_clarification = PendingClarification(
                    clarify_id=event["clarify_id"],
                    question=event["question"],
                    choices=tuple(event["choices"]),
                )
                pending.clarification_state_changed.clear()

            if pending.queue.full():
                self._finish_clarification(
                    pending,
                    error=GatewayClarificationConflictError(
                        "Clarification ended because the event buffer overflowed"
                    ),
                )
                self._abort_approvals(
                    pending,
                    error=GatewayApprovalConflictError(
                        "Approval ended because the event buffer overflowed"
                    ),
                )
                if event_type in TERMINAL_EVENT_TYPES:
                    self._replace_queue_with_event(pending, event)
                    pending.terminal_received = True
                    pending.accepting_events = False
                    return
                pending.accepting_events = False
                self._replace_queue_with_event(
                    pending,
                    {
                        "type": "request.failed",
                        "request_id": pending.request_id,
                        "session_id": pending.session_id,
                        "error": "Gateway event buffer exceeded its limit",
                    },
                )
                return

            if event_type in TERMINAL_EVENT_TYPES:
                pending.terminal_received = True
                pending.accepting_events = False
                self._finish_clarification(
                    pending,
                    error=GatewayClarificationConflictError(
                        "Agent request ended before clarification response acknowledgement"
                    ),
                )
                self._abort_approvals(
                    pending,
                    error=GatewayApprovalConflictError(
                        "Agent request ended before approval response acknowledgement"
                    ),
                )
            pending.queue.put_nowait(event)

    async def respond_clarify(
        self,
        request_id: str,
        clarify_id: str,
        owner_client_id: str,
        response: str,
    ) -> None:
        if not isinstance(response, str) or not response.strip():
            raise GatewayProtocolError("Clarification response must not be empty")

        async with self._state_lock:
            pending = self._pending_by_request.get(request_id)
            if pending is None or pending.owner_client_id != owner_client_id:
                raise GatewayClarificationNotFoundError(
                    "Gateway request was not found"
                )
            clarification = pending.current_clarification
            if (
                pending.terminal_received
                or not pending.accepting_events
                or clarification is None
                or pending.clarification_response_waiter is not None
            ):
                raise GatewayClarificationConflictError(
                    "No unanswered clarification exists for this request"
                )
            if clarification.clarify_id != clarify_id:
                raise GatewayClarificationConflictError(
                    "Clarification id does not match the active clarification"
                )
            websocket = self._websocket
            if websocket is None:
                raise GatewayUnavailableError(
                    "Hermes Gateway bridge is not connected"
                )
            waiter: asyncio.Future[ClarificationResponseOutcome] = (
                asyncio.get_running_loop().create_future()
            )
            pending.clarification_response_waiter = waiter
            frame = {
                "type": "clarify.respond",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarification.clarify_id,
                "response": response.strip(),
            }

        send_task: asyncio.Task[None] | None = None
        cancelled_during_send: asyncio.CancelledError | None = None
        try:
            async with self._send_lock:
                async with self._state_lock:
                    still_registered = (
                        self._pending_by_request.get(pending.request_id) is pending
                        and pending.clarification_response_waiter is waiter
                        and self._websocket is websocket
                    )
                if not still_registered:
                    outcome = await asyncio.shield(waiter)
                    if outcome.error is not None:
                        raise outcome.error
                    raise GatewayClarificationConflictError(
                        "Clarification response is no longer active"
                    )
                send_task = asyncio.create_task(websocket.send_json(frame))
                try:
                    await asyncio.shield(send_task)
                except asyncio.CancelledError as exc:
                    cancelled_during_send = exc
                    try:
                        await asyncio.shield(send_task)
                    except asyncio.CancelledError:
                        await send_task
                    except (OSError, RuntimeError, WebSocketDisconnect):
                        await self.detach(
                            websocket,
                            "Gateway bridge disconnected while answering clarification",
                        )
        except asyncio.CancelledError:
            if send_task is None:
                await self._expire_clarification_waiter(pending, waiter)
            else:
                await self._wait_for_ack_after_cancellation(pending, waiter)
            raise
        except (OSError, RuntimeError, WebSocketDisconnect) as exc:
            await self.detach(
                websocket,
                "Gateway bridge disconnected while answering clarification",
            )
            raise GatewayUnavailableError(
                "Hermes Gateway bridge disconnected while answering clarification"
            ) from exc

        if cancelled_during_send is not None:
            await self._wait_for_ack_after_cancellation(pending, waiter)
            raise cancelled_during_send

        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=self._clarification_ack_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._expire_clarification_waiter(pending, waiter)
            raise GatewayClarificationConflictError(
                "Clarification response acknowledgement timed out"
            ) from exc
        except asyncio.CancelledError:
            await self._wait_for_ack_after_cancellation(pending, waiter)
            raise

        if outcome.error is not None:
            raise outcome.error
        if not outcome.accepted:
            raise GatewayClarificationConflictError(
                "Hermes Gateway rejected clarification response"
            )

    async def respond_approval(
        self,
        request_id: str,
        approval_id: str,
        owner_client_id: str,
        choice: str,
    ) -> None:
        if choice not in APPROVAL_CHOICES:
            raise GatewayProtocolError(
                "Approval choice must be one of once, session, always, deny"
            )

        async with self._state_lock:
            pending = self._pending_by_request.get(request_id)
            if pending is None or pending.owner_client_id != owner_client_id:
                raise GatewayApprovalNotFoundError("Gateway request was not found")
            current_approval = (
                pending.pending_approvals[0]
                if pending.pending_approvals
                else None
            )
            if (
                pending.terminal_received
                or not pending.accepting_events
                or current_approval is None
                or pending.approval_response_waiter is not None
            ):
                raise GatewayApprovalConflictError(
                    "No unanswered approval exists for this request"
                )
            if current_approval.approval_id != approval_id:
                raise GatewayApprovalConflictError(
                    "Approval id does not match the first pending approval"
                )
            if not current_approval.allows(choice):
                raise GatewayApprovalConflictError(
                    "Approval choice is not allowed for this operation"
                )
            websocket = self._websocket
            if websocket is None:
                raise GatewayUnavailableError(
                    "Hermes Gateway bridge is not connected"
                )
            waiter: asyncio.Future[ApprovalResponseOutcome] = (
                asyncio.get_running_loop().create_future()
            )
            pending.approval_response_waiter = waiter
            frame = {
                "type": "approval.respond",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": current_approval.approval_id,
                "choice": choice,
            }

        send_task: asyncio.Task[None] | None = None
        cancelled_during_send: asyncio.CancelledError | None = None
        try:
            async with self._send_lock:
                async with self._state_lock:
                    still_registered = (
                        self._pending_by_request.get(pending.request_id) is pending
                        and pending.approval_response_waiter is waiter
                        and self._websocket is websocket
                    )
                if not still_registered:
                    outcome = await asyncio.shield(waiter)
                    if outcome.error is not None:
                        raise outcome.error
                    raise GatewayApprovalConflictError(
                        "Approval response is no longer active"
                    )
                send_task = asyncio.create_task(websocket.send_json(frame))
                try:
                    await asyncio.shield(send_task)
                except asyncio.CancelledError as exc:
                    cancelled_during_send = exc
                    try:
                        await asyncio.shield(send_task)
                    except asyncio.CancelledError:
                        await send_task
                    except (OSError, RuntimeError, WebSocketDisconnect):
                        await self.detach(
                            websocket,
                            "Gateway bridge disconnected while answering approval",
                        )
        except asyncio.CancelledError:
            if send_task is None:
                await self._expire_approval_waiter(pending, waiter)
            else:
                await self._wait_for_approval_ack_after_cancellation(
                    pending,
                    waiter,
                )
            raise
        except (OSError, RuntimeError, WebSocketDisconnect) as exc:
            await self.detach(
                websocket,
                "Gateway bridge disconnected while answering approval",
            )
            raise GatewayUnavailableError(
                "Hermes Gateway bridge disconnected while answering approval"
            ) from exc

        if cancelled_during_send is not None:
            await self._wait_for_approval_ack_after_cancellation(
                pending,
                waiter,
            )
            raise cancelled_during_send

        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=self._approval_ack_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._expire_approval_waiter(pending, waiter)
            raise GatewayApprovalConflictError(
                "Approval response acknowledgement timed out"
            ) from exc
        except asyncio.CancelledError:
            await self._wait_for_approval_ack_after_cancellation(
                pending,
                waiter,
            )
            raise

        if outcome.error is not None:
            raise outcome.error
        if not outcome.accepted:
            raise GatewayApprovalConflictError(
                "Hermes Gateway rejected approval response"
            )

    async def iter_events(self, pending: PendingGatewayRequest):
        while True:
            clarification_active = pending.current_clarification is not None
            approval_active = bool(pending.pending_approvals)
            if clarification_active:
                timeout_seconds = self._clarification_timeout_seconds
                state_changed = pending.clarification_state_changed
            elif approval_active:
                timeout_seconds = self._approval_timeout_seconds
                state_changed = pending.approval_state_changed
            else:
                timeout_seconds = self._request_timeout_seconds
                state_changed = None
            try:
                event = await self._next_event(
                    pending,
                    timeout_seconds=timeout_seconds,
                    state_changed=state_changed,
                )
            except TimeoutError:
                await self.release_request(pending, notify_gateway=True)
                yield {
                    "type": "request.failed",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "error": (
                        "Hermes Gateway clarification timed out"
                        if clarification_active
                        else (
                            "Hermes Gateway approval timed out"
                            if approval_active
                            else "Hermes Gateway request timed out"
                        )
                    ),
                }
                return
            if event is None:
                continue
            yield event
            if event["type"] in TERMINAL_EVENT_TYPES:
                return

    async def release_request(
        self,
        pending: PendingGatewayRequest,
        *,
        notify_gateway: bool,
    ) -> None:
        async with self._state_lock:
            registered = self._pending_by_request.get(pending.request_id)
            if registered is not pending:
                return
            del self._pending_by_request[pending.request_id]
            self._request_by_session.pop(pending.session_id, None)
            self._finish_clarification(
                pending,
                error=GatewayClarificationConflictError(
                    "Clarification request was released before response acknowledgement"
                ),
            )
            self._abort_approvals(
                pending,
                error=GatewayApprovalConflictError(
                    "Approval request was released before response acknowledgement"
                ),
            )
            websocket = self._websocket

        if not notify_gateway or pending.terminal_received or websocket is None:
            return
        cancel_frame = {
            "type": "request.cancel",
            "request_id": pending.request_id,
            "session_id": pending.session_id,
        }
        try:
            async with self._send_lock:
                await websocket.send_json(cancel_frame)
        except (OSError, RuntimeError, WebSocketDisconnect):
            await self.detach(websocket, "Gateway bridge disconnected while cancelling request")

    async def _rollback_start(
        self,
        pending: PendingGatewayRequest,
        websocket: GatewaySocket,
        send_started: bool,
    ) -> None:
        async with self._state_lock:
            registered = self._pending_by_request.get(pending.request_id)
            if registered is not pending:
                return
            del self._pending_by_request[pending.request_id]
            self._request_by_session.pop(pending.session_id, None)
            websocket_is_current = self._websocket is websocket

        if not send_started or not websocket_is_current:
            return
        cancel_frame = {
            "type": "request.cancel",
            "request_id": pending.request_id,
            "session_id": pending.session_id,
        }
        try:
            async with self._send_lock:
                await websocket.send_json(cancel_frame)
        except (OSError, RuntimeError, WebSocketDisconnect):
            await self.detach(websocket, "Gateway bridge disconnected while rolling back request")

    @staticmethod
    def _replace_queue_with_event(
        pending: PendingGatewayRequest,
        event: dict[str, Any],
    ) -> None:
        while not pending.queue.empty():
            pending.queue.get_nowait()
        pending.queue.put_nowait(event)

    @staticmethod
    def _finish_clarification(
        pending: PendingGatewayRequest,
        *,
        accepted: bool = False,
        error: GatewayBridgeError | None,
    ) -> None:
        waiter = pending.clarification_response_waiter
        if pending.current_clarification is not None:
            pending.last_settled_clarify_id = (
                pending.current_clarification.clarify_id
            )
        pending.clarification_response_waiter = None
        pending.current_clarification = None
        pending.clarification_state_changed.set()
        if waiter is not None and not waiter.done():
            waiter.set_result(
                ClarificationResponseOutcome(
                    accepted=accepted,
                    error=error,
                )
            )

    async def _expire_clarification_waiter(
        self,
        pending: PendingGatewayRequest,
        waiter: asyncio.Future[ClarificationResponseOutcome],
    ) -> None:
        async with self._state_lock:
            if pending.clarification_response_waiter is not waiter:
                return
            self._finish_clarification(
                pending,
                error=GatewayClarificationConflictError(
                    "Clarification response is no longer active"
                ),
            )

    async def _wait_for_ack_after_cancellation(
        self,
        pending: PendingGatewayRequest,
        waiter: asyncio.Future[ClarificationResponseOutcome],
    ) -> None:
        cleanup_task = asyncio.create_task(
            self._wait_for_ack_or_expire(pending, waiter)
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task

    async def _wait_for_ack_or_expire(
        self,
        pending: PendingGatewayRequest,
        waiter: asyncio.Future[ClarificationResponseOutcome],
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=self._clarification_ack_timeout_seconds,
            )
        except TimeoutError:
            await self._expire_clarification_waiter(pending, waiter)

    @staticmethod
    def _finish_current_approval(
        pending: PendingGatewayRequest,
        *,
        accepted: bool = False,
        error: GatewayBridgeError | None,
    ) -> None:
        waiter = pending.approval_response_waiter
        if not pending.pending_approvals:
            raise RuntimeError("Cannot finish approval without a pending approval")
        approval = pending.pending_approvals.pop(0)
        pending.settled_approval_ids.add(approval.approval_id)
        pending.approval_response_waiter = None
        pending.approval_state_changed.set()
        if waiter is not None and not waiter.done():
            waiter.set_result(
                ApprovalResponseOutcome(
                    accepted=accepted,
                    error=error,
                )
            )

    @staticmethod
    def _abort_approvals(
        pending: PendingGatewayRequest,
        *,
        error: GatewayBridgeError,
    ) -> None:
        waiter = pending.approval_response_waiter
        for approval in pending.pending_approvals:
            pending.settled_approval_ids.add(approval.approval_id)
        pending.pending_approvals.clear()
        pending.approval_response_waiter = None
        pending.approval_state_changed.set()
        if waiter is not None and not waiter.done():
            waiter.set_result(
                ApprovalResponseOutcome(
                    accepted=False,
                    error=error,
                )
            )

    async def _expire_approval_waiter(
        self,
        pending: PendingGatewayRequest,
        waiter: asyncio.Future[ApprovalResponseOutcome],
    ) -> None:
        async with self._state_lock:
            if pending.approval_response_waiter is not waiter:
                return
            self._finish_current_approval(
                pending,
                error=GatewayApprovalConflictError(
                    "Approval response is no longer active"
                ),
            )

    async def _wait_for_approval_ack_after_cancellation(
        self,
        pending: PendingGatewayRequest,
        waiter: asyncio.Future[ApprovalResponseOutcome],
    ) -> None:
        cleanup_task = asyncio.create_task(
            self._wait_for_approval_ack_or_expire(pending, waiter)
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task

    async def _wait_for_approval_ack_or_expire(
        self,
        pending: PendingGatewayRequest,
        waiter: asyncio.Future[ApprovalResponseOutcome],
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=self._approval_ack_timeout_seconds,
            )
        except TimeoutError:
            await self._expire_approval_waiter(pending, waiter)

    @staticmethod
    async def _next_event(
        pending: PendingGatewayRequest,
        *,
        timeout_seconds: float,
        state_changed: asyncio.Event | None,
    ) -> dict[str, Any] | None:
        queue_task = asyncio.create_task(pending.queue.get())
        state_task = (
            asyncio.create_task(state_changed.wait())
            if state_changed is not None
            else None
        )
        tasks = {queue_task}
        if state_task is not None:
            tasks.add(state_task)
        try:
            done, unfinished = await asyncio.wait(
                tasks,
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise
        for task in unfinished:
            task.cancel()
        for task in unfinished:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not done:
            raise TimeoutError
        if state_task is not None and state_task in done:
            state_changed.clear()
        if queue_task in done:
            return queue_task.result()
        return None

    @staticmethod
    def _validate_event(frame: object) -> dict[str, Any]:
        if not isinstance(frame, dict):
            raise GatewayProtocolError("Gateway event must be a JSON object")
        event_type = frame.get("type")
        if event_type not in GATEWAY_EVENT_TYPES:
            raise GatewayProtocolError("Gateway event type is not supported")
        request_id = frame.get("request_id")
        session_id = frame.get("session_id")
        if not isinstance(request_id, str) or not request_id:
            raise GatewayProtocolError("Gateway event request_id is required")
        if not isinstance(session_id, str) or not session_id:
            raise GatewayProtocolError("Gateway event session_id is required")

        if event_type in {"assistant.snapshot", "reasoning"}:
            text = frame.get("text")
            if not isinstance(text, str):
                raise GatewayProtocolError(f"Gateway {event_type} event text is required")
            maximum_bytes = (
                MAX_REASONING_TEXT_BYTES
                if event_type == "reasoning"
                else MAX_ASSISTANT_TEXT_BYTES
            )
            if len(text.encode("utf-8")) > maximum_bytes:
                raise GatewayProtocolError(
                    f"Gateway {event_type} event text exceeds the maximum size"
                )
        elif event_type in {"tool.started", "tool.completed"}:
            if not isinstance(frame.get("name"), str) or not frame["name"]:
                raise GatewayProtocolError(f"Gateway {event_type} event name is required")
        elif event_type == "request.completed":
            if "text" in frame and not isinstance(frame["text"], str):
                raise GatewayProtocolError("Gateway request.completed text must be a string")
            if (
                "text" in frame
                and len(frame["text"].encode("utf-8")) > MAX_ASSISTANT_TEXT_BYTES
            ):
                raise GatewayProtocolError(
                    "Gateway request.completed event text exceeds the maximum size"
                )
        elif event_type == "request.failed":
            if not isinstance(frame.get("error"), str) or not frame["error"]:
                raise GatewayProtocolError("Gateway request.failed error is required")
        elif event_type == "approval.request":
            approval_id = frame.get("approval_id")
            command = frame.get("command")
            description = frame.get("description")
            if (
                not isinstance(approval_id, str)
                or not valid_gateway_approval_id(approval_id)
            ):
                raise GatewayProtocolError(
                    "Gateway approval.request approval_id is required"
                )
            if (
                not isinstance(command, str)
                or not command.strip()
                or len(command) > MAX_APPROVAL_COMMAND_LENGTH
            ):
                raise GatewayProtocolError(
                    "Gateway approval.request command is invalid"
                )
            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > MAX_APPROVAL_DESCRIPTION_LENGTH
            ):
                raise GatewayProtocolError(
                    "Gateway approval.request description is invalid"
                )
            for field_name in (
                "allow_permanent",
                "allow_session",
                "smart_denied",
            ):
                if type(frame.get(field_name)) is not bool:
                    raise GatewayProtocolError(
                        f"Gateway approval.request {field_name} must be a boolean"
                    )
        elif event_type == "approval.response":
            approval_id = frame.get("approval_id")
            if (
                not isinstance(approval_id, str)
                or not valid_gateway_approval_id(approval_id)
            ):
                raise GatewayProtocolError(
                    "Gateway approval.response approval_id is required"
                )
            if not isinstance(frame.get("accepted"), bool):
                raise GatewayProtocolError(
                    "Gateway approval.response accepted must be a boolean"
                )
        elif event_type == "clarify.request":
            clarify_id = frame.get("clarify_id")
            question = frame.get("question")
            choices = frame.get("choices")
            if (
                not isinstance(clarify_id, str)
                or not valid_gateway_clarify_id(clarify_id)
            ):
                raise GatewayProtocolError(
                    "Gateway clarify.request clarify_id is required"
                )
            if (
                not isinstance(question, str)
                or not question.strip()
                or len(question) > MAX_CLARIFY_QUESTION_LENGTH
            ):
                raise GatewayProtocolError(
                    "Gateway clarify.request question is invalid"
                )
            if not isinstance(choices, list) or len(choices) > MAX_CLARIFY_CHOICES:
                raise GatewayProtocolError(
                    "Gateway clarify.request choices must be a bounded list"
                )
            if any(
                not isinstance(choice, str)
                or not choice.strip()
                or len(choice) > MAX_CLARIFY_CHOICE_LENGTH
                for choice in choices
            ):
                raise GatewayProtocolError(
                    "Gateway clarify.request choice is invalid"
                )
        elif event_type == "clarify.response":
            clarify_id = frame.get("clarify_id")
            if (
                not isinstance(clarify_id, str)
                or not valid_gateway_clarify_id(clarify_id)
            ):
                raise GatewayProtocolError(
                    "Gateway clarify.response clarify_id is required"
                )
            if not isinstance(frame.get("accepted"), bool):
                raise GatewayProtocolError(
                    "Gateway clarify.response accepted must be a boolean"
                )
        return frame
