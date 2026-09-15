from __future__ import annotations

from ipaddress import IPv4Address
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

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


class WebUIFeedbackSnapshotMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    payload: dict[str, Any] | None = None


class WebUIMessageFeedbackRequest(BaseModel):
    source_instance_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    feedback: Literal["like", "dislike"]
    messages: list[WebUIFeedbackSnapshotMessage] = Field(min_length=1)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ip: str

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        return normalize_node_ip(value)


_REMOTE_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


class LogMonitorPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    target_ip: str
    ssh_port: int = Field(ge=1, le=65535)
    ssh_username: str
    ssh_password: SecretStr = Field(min_length=1, max_length=1024, repr=False)
    stream_log_monitor: Literal["1"] = Field(alias="STREAM_LOG_MONITOR")
    mcp_iadebug_user: str = Field(alias="MCP_IADEBUG_USER")
    active_home: str = Field(alias="ACTIVE_HOME")
    stream_home: str = Field(alias="STREAM_HOME")
    stream_data_home: str = Field(alias="STREAM_DATA_HOME")

    @field_validator("target_ip")
    @classmethod
    def validate_target_ip(cls, value: str) -> str:
        try:
            address = IPv4Address(value)
        except ValueError:
            raise ValueError("target_ip must be an IPv4 address") from None
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("target_ip must identify a remote unicast host")
        return str(address)

    @field_validator("ssh_username", "mcp_iadebug_user")
    @classmethod
    def validate_remote_username(cls, value: str) -> str:
        if not _REMOTE_USERNAME_RE.fullmatch(value):
            raise ValueError("username contains unsupported characters")
        return value

    @field_validator("ssh_password")
    @classmethod
    def validate_ssh_password(cls, value: SecretStr) -> SecretStr:
        if "\x00" in value.get_secret_value():
            raise ValueError("ssh_password must not contain a NUL byte")
        return value

    @field_validator("active_home", "stream_home", "stream_data_home")
    @classmethod
    def validate_remote_path(cls, value: str) -> str:
        if len(value) > 512 or not _REMOTE_PATH_RE.fullmatch(value):
            raise ValueError("path must be an absolute path using only letters, digits, _, -, . and /")
        path = PurePosixPath(value)
        if any(part in {".", ".."} for part in path.parts) or "//" in value:
            raise ValueError("path must not contain dot segments or repeated separators")
        normalized = str(path)
        if normalized == "/" or normalized != value or value.endswith("/"):
            raise ValueError("path must be a normalized non-root absolute path")
        return normalized


class LogMonitorInstallRequest(LogMonitorPreflightRequest):
    preflight_id: str = Field(pattern=r"^[0-9a-f]{32}$")


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
