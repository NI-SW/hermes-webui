from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr


class AgentTaskError(RuntimeError):
    """后台 Agent 任务无法产出可用终态。"""


class HermesResponsesAgent:
    """通过普通 Hermes Responses API 执行一次无状态后台总结。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Hermes API base URL must not be blank")
        if not model.strip():
            raise ValueError("Hermes API model must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("Hermes API timeout must be positive")
        self._responses_url = f"{base_url.rstrip('/')}/v1/responses"
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def run_prompt(self, *, task_id: str, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("Agent task prompt must not be blank")
        api_key = self._api_key.get_secret_value()
        if not api_key:
            raise AgentTaskError("Hermes API key is not configured")

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._responses_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Hermes-Session-Key": f"datacop-summary:{task_id}",
                    },
                    json={
                        "model": self._model,
                        "input": prompt,
                        "store": False,
                        "stream": False,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AgentTaskError(
                f"Hermes API returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentTaskError("Hermes API request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentTaskError("Hermes API returned invalid JSON") from exc
        text = _extract_output_text(payload)
        if not text.strip():
            raise AgentTaskError("Agent task completed with empty final text")
        return text


def _extract_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise AgentTaskError("Hermes API response must be an object")
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text

    pieces: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
    return "".join(pieces)
