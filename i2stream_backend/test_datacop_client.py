from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import SecretStr, ValidationError

import datacop_client
from datacop_client import DatacopClient, DatacopClientError, DatacopProblem


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeAsyncClient:
    responses: list[FakeResponse] = []
    requests: list[tuple[str, str, dict[str, object], dict[str, str] | None]] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.requests.append(("POST", url, json, headers))
        return self.responses.pop(0)


class DatacopProblemTests(unittest.TestCase):
    valid_problem = {
        "name": "规则同步失败",
        "description": "规则无法同步",
        "scenario": "同步规则",
        "trigger_method": "执行同步",
        "symptoms": "任务失败",
        "cause": "配置缺失",
        "solution": "补充配置",
        "verification": "重新同步成功",
        "notes": "",
    }

    def test_problem_requires_exact_datacop_shape(self) -> None:
        problem = DatacopProblem.model_validate(self.valid_problem)
        self.assertEqual(problem.name, "规则同步失败")
        self.assertEqual(problem.model_dump(), self.valid_problem)

        for invalid in (
            {**self.valid_problem, "name": " "},
            {**self.valid_problem, "extra": "not allowed"},
            {key: value for key, value in self.valid_problem.items() if key != "cause"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    DatacopProblem.model_validate(invalid)

    def test_problem_json_does_not_accept_markdown_fences(self) -> None:
        with self.assertRaises(ValidationError):
            DatacopProblem.model_validate_json(
                '```json\n{"name":"test"}\n```',
            )


class DatacopClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeAsyncClient.responses = []
        FakeAsyncClient.requests = []
        self.client = DatacopClient(
            base_url="http://192.0.2.10:5173/api",
            project_id=7,
            username="service-account",
            password=SecretStr("secret-password"),
            timeout_seconds=10,
        )
        self.problem = DatacopProblem.model_validate(DatacopProblemTests.valid_problem)

    async def test_upload_logs_in_and_sends_only_whitelisted_problem_fields(self) -> None:
        FakeAsyncClient.responses = [
            FakeResponse(200, {"token": "jwt-token", "user": {"id": 2}}),
            FakeResponse(201, {"id": 91, "message": "问题上传成功"}),
        ]

        with patch.object(datacop_client.httpx, "AsyncClient", FakeAsyncClient):
            problem_id = await self.client.upload_problem(self.problem)

        self.assertEqual(problem_id, 91)
        self.assertEqual(FakeAsyncClient.requests[0][1], "http://192.0.2.10:5173/api/auth/login")
        upload_request = FakeAsyncClient.requests[1]
        self.assertEqual(upload_request[1], "http://192.0.2.10:5173/api/projects/7/problems")
        self.assertEqual(
            upload_request[2],
            DatacopProblemTests.valid_problem,
        )
        self.assertEqual(upload_request[3], {"Authorization": "Bearer jwt-token"})

    async def test_upload_does_not_retry_after_unauthorized_response(self) -> None:
        FakeAsyncClient.responses = [
            FakeResponse(200, {"token": "first-token"}),
            FakeResponse(401, {"error": "expired"}),
        ]

        with patch.object(datacop_client.httpx, "AsyncClient", FakeAsyncClient):
            with self.assertRaisesRegex(DatacopClientError, "HTTP 401"):
                await self.client.upload_problem(self.problem)

        self.assertEqual(len(FakeAsyncClient.requests), 2)
        self.assertEqual(
            FakeAsyncClient.requests[-1][3],
            {"Authorization": "Bearer first-token"},
        )

    async def test_invalid_login_or_upload_response_fails_explicitly(self) -> None:
        cases = (
            ([FakeResponse(200, {})], "missing token"),
            (
                [FakeResponse(200, {"token": "token"}), FakeResponse(201, {"message": "missing id"})],
                "missing problem id",
            ),
        )
        for responses, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                FakeAsyncClient.responses = list(responses)
                FakeAsyncClient.requests = []
                client = DatacopClient(
                    base_url="http://192.0.2.10:5173/api",
                    project_id=7,
                    username="service-account",
                    password=SecretStr("secret-password"),
                    timeout_seconds=10,
                )
                with patch.object(datacop_client.httpx, "AsyncClient", FakeAsyncClient):
                    with self.assertRaisesRegex(DatacopClientError, expected_error):
                        await client.upload_problem(self.problem)


if __name__ == "__main__":
    unittest.main()
