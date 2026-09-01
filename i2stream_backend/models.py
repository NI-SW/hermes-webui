from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from node_store import normalize_node_ip


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]


class CommunicateRequest(BaseModel):
    agent_port: int = Field(gt=0, le=65535)
    conversation_id: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    api_key: str = Field(min_length=1, repr=False)
    stream: bool = True
    model: str | None = None


class ClarificationResponse(BaseModel):
    response: str = Field(min_length=1, max_length=10_000)


class ApprovalResponse(BaseModel):
    choice: Literal["once", "session", "always", "deny"]


class DashboardSessionCreateRequest(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title must not be blank")
        return title


class DashboardSessionUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title must not be blank")
        return title


class DashboardRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be blank")
        return message


class MessageFeedbackRequest(BaseModel):
    feedback: Literal["like", "dislike"]


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ip: str

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        return normalize_node_ip(value)


class FileRecord(BaseModel):
    token: str
    filename: str
    path: Path
    media_type: str
    description: str = ""


class ChatMessageRecord(BaseModel):
    role: Literal["system", "user", "assistant", "error", "file"]
    content: str = ""
    payload: dict[str, Any] | None = None
