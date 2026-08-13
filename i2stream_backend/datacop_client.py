from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class DatacopClientError(RuntimeError):
    """DataCop 认证、传输或响应契约错误。"""


class DatacopProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(max_length=20_000)
    scenario: str = Field(max_length=20_000)
    trigger_method: str = Field(max_length=20_000)
    symptoms: str = Field(max_length=20_000)
    cause: str = Field(max_length=20_000)
    solution: str = Field(max_length=20_000)
    verification: str = Field(max_length=20_000)
    notes: str = Field(max_length=20_000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class DatacopClient:
    def __init__(
        self,
        *,
        base_url: str,
        project_id: int,
        username: str,
        password: SecretStr,
        timeout_seconds: float,
    ) -> None:
        if not base_url or not username or not password.get_secret_value():
            raise ValueError("DataCop client configuration must be complete")
        if project_id <= 0 or timeout_seconds <= 0:
            raise ValueError("DataCop project id and timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._project_id = project_id
        self._username = username
        self._password = password
        self._timeout = httpx.Timeout(timeout_seconds)
        self._token: str | None = None

    async def upload_problem(self, problem: DatacopProblem) -> int:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            token = self._token or await self._login(client)
            response = await self._post_problem(client, problem, token)
            if response.status_code == 401:
                self._token = None

        if response.status_code not in {200, 201}:
            raise DatacopClientError(
                f"DataCop problem upload failed with HTTP {response.status_code}"
            )
        payload = self._json_object(response, "problem upload")
        problem_id = payload.get("id")
        if not isinstance(problem_id, int) or problem_id <= 0:
            raise DatacopClientError("DataCop problem upload response missing problem id")
        return problem_id

    async def _login(self, client: httpx.AsyncClient) -> str:
        try:
            response = await client.post(
                f"{self._base_url}/auth/login",
                json={
                    "username": self._username,
                    "password": self._password.get_secret_value(),
                },
            )
        except httpx.RequestError as exc:
            raise DatacopClientError("DataCop login request failed") from exc
        if response.status_code != 200:
            raise DatacopClientError(
                f"DataCop login failed with HTTP {response.status_code}"
            )
        payload = self._json_object(response, "login")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise DatacopClientError("DataCop login response missing token")
        self._token = token
        return token

    async def _post_problem(
        self,
        client: httpx.AsyncClient,
        problem: DatacopProblem,
        token: str,
    ) -> httpx.Response:
        try:
            return await client.post(
                f"{self._base_url}/projects/{self._project_id}/problems",
                json=problem.model_dump(),
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.RequestError as exc:
            raise DatacopClientError("DataCop problem upload request failed") from exc

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DatacopClientError(
                f"DataCop {operation} response must be valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DatacopClientError(
                f"DataCop {operation} response must be a JSON object"
            )
        return payload
