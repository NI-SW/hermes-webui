from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr

from agent_task import AgentTaskError, HermesResponsesAgent


class FakeAsyncClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.post = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class AgentTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_uses_stateless_hermes_responses_api(self) -> None:
        request = httpx.Request("POST", "http://192.168.34.65:8642/v1/responses")
        response = httpx.Response(
            200,
            request=request,
            json={"output_text": '{"name":"test"}'},
        )
        fake_client = FakeAsyncClient(response)
        runner = HermesResponsesAgent(
            base_url="http://192.168.34.65:8642",
            api_key=SecretStr("api-key-for-tests"),
            model="hermes-agent",
            timeout_seconds=30,
        )

        with patch("agent_task.httpx.AsyncClient", return_value=fake_client) as client_type:
            result = await runner.run_prompt(
                task_id="a" * 32,
                prompt="summarize this conversation",
            )

        self.assertEqual(result, '{"name":"test"}')
        client_type.assert_called_once_with(timeout=30, trust_env=False)
        fake_client.post.assert_awaited_once_with(
            "http://192.168.34.65:8642/v1/responses",
            headers={
                "Authorization": "Bearer api-key-for-tests",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Hermes-Session-Key": "datacop-summary:" + "a" * 32,
            },
            json={
                "model": "hermes-agent",
                "input": "summarize this conversation",
                "store": False,
                "stream": False,
            },
        )

    async def test_http_failure_does_not_expose_remote_response_body(self) -> None:
        request = httpx.Request("POST", "http://192.168.34.65:8642/v1/responses")
        response = httpx.Response(
            500,
            request=request,
            text="private upstream stack trace",
        )
        fake_client = FakeAsyncClient(response)
        runner = HermesResponsesAgent(
            base_url="http://192.168.34.65:8642",
            api_key=SecretStr("api-key-for-tests"),
            model="hermes-agent",
            timeout_seconds=30,
        )

        with patch("agent_task.httpx.AsyncClient", return_value=fake_client):
            with self.assertRaisesRegex(AgentTaskError, "HTTP 500") as raised:
                await runner.run_prompt(task_id="b" * 32, prompt="summarize")

        self.assertNotIn("private upstream", str(raised.exception))

    async def test_missing_api_key_and_empty_output_fail_closed(self) -> None:
        runner = HermesResponsesAgent(
            base_url="http://192.168.34.65:8642",
            api_key=SecretStr(""),
            model="hermes-agent",
            timeout_seconds=30,
        )
        with self.assertRaisesRegex(AgentTaskError, "API key is not configured"):
            await runner.run_prompt(task_id="c" * 32, prompt="summarize")

        request = httpx.Request("POST", "http://192.168.34.65:8642/v1/responses")
        response = httpx.Response(200, request=request, json={"output": []})
        fake_client = FakeAsyncClient(response)
        configured = HermesResponsesAgent(
            base_url="http://192.168.34.65:8642",
            api_key=SecretStr("api-key-for-tests"),
            model="hermes-agent",
            timeout_seconds=30,
        )
        with patch("agent_task.httpx.AsyncClient", return_value=fake_client):
            with self.assertRaisesRegex(AgentTaskError, "empty final text"):
                await configured.run_prompt(task_id="d" * 32, prompt="summarize")


if __name__ == "__main__":
    unittest.main()
