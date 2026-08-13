from __future__ import annotations

from typing import Any, Protocol

from pydantic import SecretStr

from gateway_bridge import GatewayBridge, derive_gateway_session_id


class AgentTaskError(RuntimeError):
    """后台 Agent 任务无法产出可用终态。"""


class AgentBridge(Protocol):
    async def start_request(
        self,
        session_id: str,
        payload: dict[str, Any],
        owner_client_id: str,
    ) -> Any: ...

    def iter_events(self, pending: Any) -> Any: ...

    async def release_request(self, pending: Any, *, notify_gateway: bool) -> None: ...


async def run_agent_prompt(
    bridge: GatewayBridge | AgentBridge,
    session_secret: SecretStr,
    *,
    task_id: str,
    prompt: str,
) -> str:
    if not prompt.strip():
        raise ValueError("Agent task prompt must not be blank")

    session_id = derive_gateway_session_id(
        "internal-datacop-summary-session",
        task_id,
        session_secret,
    )
    owner_id = derive_gateway_session_id(
        "internal-datacop-summary-owner",
        task_id,
        session_secret,
    )
    pending = await bridge.start_request(
        session_id,
        {"input": prompt},
        owner_client_id=owner_id,
    )
    try:
        async for event in bridge.iter_events(pending):
            event_type = event["type"]
            if event_type == "request.completed":
                text = event["text"]
                if not isinstance(text, str) or not text.strip():
                    raise AgentTaskError("Agent task completed with empty final text")
                return text
            if event_type == "request.failed":
                raise AgentTaskError("Agent task failed")
            if event_type == "clarify.request":
                raise AgentTaskError("Background Agent task cannot request clarification")
            if event_type == "approval.request":
                raise AgentTaskError("Background Agent task cannot request approval")
        raise AgentTaskError("Agent task ended without a terminal event")
    finally:
        await bridge.release_request(
            pending,
            notify_gateway=not pending.terminal_received,
        )
