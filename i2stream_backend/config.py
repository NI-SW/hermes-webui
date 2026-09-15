from __future__ import annotations

from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BUILTIN_DATACOP_BASE_URL = "http://192.168.34.65:5173/api"
BUILTIN_DATACOP_PROJECT_ID = 13
BUILTIN_DATACOP_USERNAME = "root"
BUILTIN_DATACOP_PASSWORD = SecretStr("admin123")
BUILTIN_DATACOP_TIMEOUT_SECONDS = 30.0
BUILTIN_DIALOG_INTERACTION_QUEUE_SIZE = 32
BUILTIN_DIALOG_INTERACTION_JOB_TTL_SECONDS = 900.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hermes_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr = SecretStr("")
    dashboard_hermes_base_url: str = "http://127.0.0.1:8641"
    dashboard_hermes_api_key: SecretStr = SecretStr("")
    default_model: str = "hermes-agent"
    request_timeout_seconds: float = 600.0
    file_store_dir: Path = Path("/app/data/agent-console/files")
    inbox_file_store_dir: Path = Path("/app/data/agent-console/inbox-files")
    public_base_url: str = ""
    proxy_api_key: str = Field(default="", repr=False)
    cors_origins: str = "*"
    vector_search_host: str
    session_hmac_secret: SecretStr
    gateway_bridge_token: SecretStr
    webui_feedback_bridge_token: SecretStr = SecretStr("")
    i2stream_install_internal_token: SecretStr = SecretStr("")
    agent_public_host: str = ""
    agent_base_url_port: int = Field(default=8642, ge=1, le=65535)
    agent_back_port: int = Field(default=50091, ge=1, le=65535)
    logmonitor_image_path: Path = Path("/app/data/image/i2up-stream-mcp.tar")
    logmonitor_start_script_path: Path = Path(
        "/app/logmonitor-installer/start_stream_mcp.sh"
    )
    logmonitor_known_hosts_path: Path = Path(
        "/app/data/agent-console/logmonitor-ssh/known_hosts"
    )
    logmonitor_preflight_ttl_seconds: int = Field(default=300, ge=30, le=1800)

    @field_validator("session_hmac_secret", "gateway_bridge_token", mode="before")
    @classmethod
    def validate_bridge_secret_length(cls, value: object) -> object:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw_value, str) or len(raw_value.encode("utf-8")) < 32:
            raise ValueError("bridge secrets must contain at least 32 UTF-8 bytes")
        return value

    @field_validator("webui_feedback_bridge_token", mode="before")
    @classmethod
    def validate_webui_feedback_bridge_token(cls, value: object) -> object:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if raw_value == "":
            return value
        if not isinstance(raw_value, str) or len(raw_value.encode("utf-8")) < 32:
            raise ValueError("configured WebUI feedback bridge token must contain at least 32 UTF-8 bytes")
        return value

    @field_validator("agent_public_host")
    @classmethod
    def validate_agent_public_host(cls, value: str) -> str:
        host = value.strip()
        if not host:
            return ""
        try:
            address = IPv4Address(host)
        except ValueError:
            raise ValueError("AGENT_PUBLIC_HOST must be an IPv4 address") from None
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("AGENT_PUBLIC_HOST must be a non-loopback unicast IPv4 address")
        return str(address)

    @field_validator("i2stream_install_internal_token", mode="before")
    @classmethod
    def validate_install_token(cls, value: object) -> object:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if raw_value == "":
            return value
        if not isinstance(raw_value, str) or len(raw_value.encode("utf-8")) < 32:
            raise ValueError("I2STREAM_INSTALL_INTERNAL_TOKEN must contain at least 32 UTF-8 bytes")
        return value

    @field_validator("hermes_base_url", "dashboard_hermes_base_url", "vector_search_host")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("vector_search_host")
    @classmethod
    def validate_vector_search_host(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("VECTOR_SEARCH_HOST must start with http:// or https://")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("VECTOR_SEARCH_HOST must be in the form http://host:port")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("VECTOR_SEARCH_HOST must not include path, query, or fragment")
        return value.rstrip("/")

    @property
    def datacop_base_url(self) -> str:
        return BUILTIN_DATACOP_BASE_URL

    @property
    def datacop_project_id(self) -> int:
        return BUILTIN_DATACOP_PROJECT_ID

    @property
    def datacop_username(self) -> str:
        return BUILTIN_DATACOP_USERNAME

    @property
    def datacop_password(self) -> SecretStr:
        return BUILTIN_DATACOP_PASSWORD

    @property
    def datacop_timeout_seconds(self) -> float:
        return BUILTIN_DATACOP_TIMEOUT_SECONDS

    @property
    def dialog_interaction_queue_size(self) -> int:
        return BUILTIN_DIALOG_INTERACTION_QUEUE_SIZE

    @property
    def dialog_interaction_job_ttl_seconds(self) -> float:
        return BUILTIN_DIALOG_INTERACTION_JOB_TTL_SECONDS

    @property
    def allowed_origins(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        origins = [origin.strip() for origin in self.cors_origins.split(",")]
        return [origin for origin in origins if origin]


settings = Settings()
