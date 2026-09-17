from __future__ import annotations

import base64
import binascii
from ipaddress import IPv4Address
from pathlib import Path
from pathlib import PurePosixPath
import re
import struct
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

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


class KnowledgeServiceConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    vector_search_host: str = Field(min_length=1, max_length=2048)
    rag_service_mcp_url: str = Field(min_length=1, max_length=2048)

    @field_validator("vector_search_host")
    @classmethod
    def validate_vector_search_host(cls, value: str) -> str:
        return _normalize_service_url(value, expected_path="", field_name="vector_search_host")

    @field_validator("rag_service_mcp_url")
    @classmethod
    def validate_rag_service_mcp_url(cls, value: str) -> str:
        return _normalize_service_url(
            value,
            expected_path="/mcp",
            field_name="rag_service_mcp_url",
        )


class KnowledgeCollectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    collection_name: str = Field(min_length=1)

    @field_validator("collection_name")
    @classmethod
    def validate_collection_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("collection_name must not be blank")
        if len(normalized) > 128:
            raise ValueError("collection_name must be at most 128 characters")
        return normalized


def _normalize_service_url(value: str, *, expected_path: str, field_name: str) -> str:
    if value != value.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{field_name} must start with http:// or https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not include user information")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} must include a valid explicit port") from None
    if not parsed.hostname or port is None:
        raise ValueError(f"{field_name} must include a host and explicit port")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not include parameters, query, or fragment")

    allowed_paths = {expected_path}
    if expected_path:
        allowed_paths.add(f"{expected_path}/")
    else:
        allowed_paths.add("/")
    if parsed.path not in allowed_paths:
        required_path = expected_path or "/"
        raise ValueError(f"{field_name} path must be {required_path}")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}{expected_path}"


_REMOTE_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


class LogMonitorPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    target_ip: str
    ssh_port: int = Field(ge=1, le=65535)
    ssh_username: str
    ssh_password: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        repr=False,
    )
    ssh_private_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=16 * 1024,
        repr=False,
    )
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
    def validate_ssh_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if "\x00" in value.get_secret_value():
            raise ValueError("ssh_password must not contain a NUL byte")
        return value

    @field_validator("ssh_private_key")
    @classmethod
    def validate_ssh_private_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        private_key = value.get_secret_value()
        if not private_key.strip():
            raise ValueError("ssh_private_key must not be blank")
        if "\x00" in private_key:
            raise ValueError("ssh_private_key must not contain a NUL byte")
        upper_key = private_key.upper()
        if (
            "-----BEGIN ENCRYPTED PRIVATE KEY-----" in upper_key
            or "PROC-TYPE: 4,ENCRYPTED" in upper_key
            or "DEK-INFO:" in upper_key
            or cls._openssh_key_is_encrypted(private_key)
        ):
            raise ValueError("encrypted SSH private keys are not supported")
        return value

    @staticmethod
    def _openssh_key_is_encrypted(private_key: str) -> bool:
        begin = "-----BEGIN OPENSSH PRIVATE KEY-----"
        end = "-----END OPENSSH PRIVATE KEY-----"
        if begin not in private_key or end not in private_key:
            return False
        encoded = private_key.split(begin, 1)[1].split(end, 1)[0]
        try:
            blob = base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, binascii.Error):
            return False
        prefix = b"openssh-key-v1\x00"
        if not blob.startswith(prefix) or len(blob) < len(prefix) + 4:
            return False
        offset = len(prefix)
        cipher_length = struct.unpack(">I", blob[offset : offset + 4])[0]
        cipher_start = offset + 4
        cipher_end = cipher_start + cipher_length
        if cipher_end > len(blob):
            return False
        return blob[cipher_start:cipher_end] != b"none"

    @model_validator(mode="after")
    def validate_ssh_credentials(self) -> "LogMonitorPreflightRequest":
        if self.ssh_password is None and self.ssh_private_key is None:
            raise ValueError("ssh_password or ssh_private_key is required")
        return self

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
