"""Configure the authenticated DataCop MCP in managed Hermes profiles."""

from __future__ import annotations

import copy
import os
import stat
import threading
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from api.config import (
    _cfg_lock as profile_config_lock,
    _load_yaml_config_file_raw as load_profile_config_raw,
    _save_yaml_config_file as save_profile_config,
    _write_yaml_config_text as restore_profile_config_text,
    reload_config as reload_active_config,
)
from api.i2stream_gateway_restart import (
    DEFAULT_GATEWAY_PROFILES,
    reset_webui_mcp_runtime,
    restart_managed_gateways,
)
from api.i2stream_mcp_config_lock import mcp_config_transaction_lock
from api.profiles import get_hermes_home_for_profile


DATACOP_MCP_SERVER_NAME = "datacop"
DATACOP_MCP_PROFILES = DEFAULT_GATEWAY_PROFILES
DATACOP_MCP_ACCEPT = "application/json, text/event-stream"
DATACOP_API_KEY_MAX_LENGTH = 4096
_DATACOP_MCP_APPLY_LOCK = threading.Lock()


def normalize_datacop_mcp_url(value: object) -> str:
    """Validate and canonicalize a DataCop Streamable HTTP MCP endpoint."""

    if not isinstance(value, str):
        raise ValueError("DataCop MCP 地址必须是字符串")
    if not value:
        raise ValueError("DataCop MCP 地址不能为空")
    if value != value.strip() or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("DataCop MCP 地址不能包含空白或控制字符")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DataCop MCP 地址格式无效") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("DataCop MCP 地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname or port is None:
        raise ValueError("DataCop MCP 地址必须包含主机和端口")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("DataCop MCP 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("DataCop MCP 地址不能包含查询参数或片段")
    if parsed.path.rstrip("/") != "/mcp":
        raise ValueError("DataCop MCP 地址路径必须是 /mcp")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}/mcp"


def validate_datacop_api_key(value: object) -> str:
    """Validate an opaque DataCop API key without assuming a vendor prefix."""

    if not isinstance(value, str):
        raise ValueError("DataCop API Key 必须是字符串")
    if not value:
        raise ValueError("DataCop API Key 不能为空")
    if len(value) > DATACOP_API_KEY_MAX_LENGTH:
        raise ValueError("DataCop API Key 过长")
    if value != value.strip() or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("DataCop API Key 不能包含空白或控制字符")
    return value


def _load_datacop_profile_config(config_path: Path) -> dict:
    """Load an existing profile strictly so malformed YAML is never overwritten."""

    if not config_path.exists():
        return load_profile_config_raw(config_path)
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read Hermes config.yaml") from exc
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Hermes profile config 无法读取：{config_path}") from exc
    try:
        loaded = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Hermes profile config 无法解析：{config_path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Hermes profile config 必须是对象：{config_path}")
    return loaded


def _datacop_server_config(url: str, api_key: str) -> dict[str, object]:
    return {
        "url": url,
        "headers": {
            "Accept": DATACOP_MCP_ACCEPT,
            "Authorization": f"Bearer {api_key}",
        },
        "enabled": True,
    }


def probe_mcp_server(name: str, config: dict) -> list[tuple[str, str]]:
    """Connect to one MCP server and return the tools it publishes."""

    from hermes_cli.mcp_config import _probe_single_server

    return _probe_single_server(name, config)


def check_datacop_mcp_connection(
    datacop_mcp_url: object,
    api_key: object,
) -> dict[str, object]:
    """Authenticate with DataCop over MCP and report only non-secret results."""

    normalized_url = normalize_datacop_mcp_url(datacop_mcp_url)
    validated_key = resolve_datacop_api_key(api_key, normalized_url)
    try:
        tools = probe_mcp_server(
            DATACOP_MCP_SERVER_NAME,
            _datacop_server_config(normalized_url, validated_key),
        )
    except Exception:
        raise RuntimeError("DataCop MCP 连接或工具探测失败") from None
    if not tools:
        raise RuntimeError("DataCop MCP 没有返回可用工具")
    return {
        "datacop_mcp_url": normalized_url,
        "tool_count": len(tools),
    }


def _profile_config_with_datacop_server(
    config: object,
    server_config: dict[str, object],
) -> dict:
    if not isinstance(config, dict):
        raise RuntimeError("Hermes profile config must be an object")
    updated = copy.deepcopy(config)
    servers = updated.get("mcp_servers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise RuntimeError("Hermes profile mcp_servers must be an object")
    servers[DATACOP_MCP_SERVER_NAME] = copy.deepcopy(server_config)
    updated["mcp_servers"] = servers
    return updated


def _restore_written_configs(
    written_paths: list[Path],
    snapshots: dict[Path, tuple[str | None, int | None]],
) -> None:
    rollback_errors: list[str] = []
    for path in reversed(written_paths):
        original, original_mode = snapshots[path]
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                restore_profile_config_text(path, original)
                if original_mode is None:
                    raise RuntimeError(f"DataCop MCP 配置缺少原始权限：{path}")
                path.chmod(original_mode)
        except (OSError, RuntimeError) as exc:
            rollback_errors.append(f"{path}: {exc}")
    if rollback_errors:
        raise RuntimeError("DataCop MCP 配置回滚失败：" + "; ".join(rollback_errors))


def _secure_profile_config(config_path: Path) -> None:
    """Restrict a profile config because it now contains an MCP bearer key."""

    config_path.chmod(0o600)


def configure_datacop_mcp_profiles(
    datacop_mcp_url: object,
    api_key: object,
) -> dict[str, object]:
    """Persist one authenticated DataCop server in every available profile."""

    normalized_url = normalize_datacop_mcp_url(datacop_mcp_url)
    validated_key = resolve_datacop_api_key(api_key, normalized_url)
    server_config = _datacop_server_config(normalized_url, validated_key)

    with profile_config_lock:
        pending: list[tuple[str, Path, dict]] = []
        missing_profiles: list[str] = []
        for profile in DATACOP_MCP_PROFILES:
            profile_home = Path(get_hermes_home_for_profile(profile))
            if profile != "default" and not profile_home.is_dir():
                missing_profiles.append(profile)
                continue
            config_path = profile_home / "config.yaml"
            current = _load_datacop_profile_config(config_path)
            pending.append(
                (
                    profile,
                    config_path,
                    _profile_config_with_datacop_server(current, server_config),
                )
            )

        snapshots = {}
        for _profile, path, _config in pending:
            if path.exists():
                snapshots[path] = (
                    path.read_text(encoding="utf-8"),
                    stat.S_IMODE(path.stat().st_mode),
                )
            else:
                snapshots[path] = (None, None)
        written_paths: list[Path] = []
        try:
            for _profile, path, config in pending:
                original, _original_mode = snapshots[path]
                if original is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    written_paths.append(path)
                    os.close(descriptor)
                else:
                    _secure_profile_config(path)
                    written_paths.append(path)
                save_profile_config(path, config)
                _secure_profile_config(path)
        except Exception as exc:
            try:
                _restore_written_configs(written_paths, snapshots)
            except RuntimeError as rollback_exc:
                raise RuntimeError(f"DataCop MCP 配置写入失败；{rollback_exc}") from exc
            raise RuntimeError("DataCop MCP 配置写入失败") from exc

    try:
        reload_active_config()
    except Exception as exc:
        raise RuntimeError("DataCop MCP 配置已写入，但 WebUI 配置重新加载失败") from exc

    return {
        "datacop_mcp_url": normalized_url,
        "configured": True,
        "has_api_key": True,
        "configured_profiles": [profile for profile, _path, _config in pending],
        "missing_profiles": missing_profiles,
        "reload_required": True,
    }


def _read_profile_datacop_server(
    config: object,
    profile: str,
) -> tuple[str, dict[str, object]] | None:
    if not isinstance(config, dict):
        raise RuntimeError(f"Hermes profile {profile} config must be an object")
    servers = config.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise RuntimeError(f"Hermes profile {profile} mcp_servers must be an object")
    server = servers.get(DATACOP_MCP_SERVER_NAME)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise RuntimeError(f"Hermes profile {profile} 的 DataCop MCP 配置无效")
    if server.get("enabled") is not True:
        raise RuntimeError(f"Hermes profile {profile} 的 DataCop MCP 未启用")

    try:
        url = normalize_datacop_mcp_url(server.get("url"))
    except ValueError as exc:
        raise RuntimeError(f"Hermes profile {profile} 的 DataCop MCP URL 无效") from exc
    headers = server.get("headers")
    if not isinstance(headers, dict):
        raise RuntimeError(f"Hermes profile {profile} 的 DataCop MCP headers 无效")
    if set(headers) != {"Accept", "Authorization"}:
        raise RuntimeError(f"Hermes profile {profile} 的 DataCop MCP headers 无效")
    if headers.get("Accept") != DATACOP_MCP_ACCEPT:
        raise RuntimeError(
            f"Hermes profile {profile} 的 DataCop MCP Accept header 无效"
        )
    authorization = headers.get("Authorization")
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise RuntimeError(
            f"Hermes profile {profile} 的 DataCop MCP Authorization header 无效"
        )
    try:
        validate_datacop_api_key(authorization.removeprefix("Bearer "))
    except ValueError as exc:
        raise RuntimeError(
            f"Hermes profile {profile} 的 DataCop MCP API Key 无效"
        ) from exc
    return url, copy.deepcopy(headers)


def _stored_datacop_api_key(expected_url: str) -> str:
    """Return the existing key only when every available profile agrees."""

    configured_value: tuple[str, dict[str, object]] | None = None
    with profile_config_lock:
        for profile in DATACOP_MCP_PROFILES:
            profile_home = Path(get_hermes_home_for_profile(profile))
            if profile != "default" and not profile_home.is_dir():
                continue
            config_path = profile_home / "config.yaml"
            profile_value = _read_profile_datacop_server(
                _load_datacop_profile_config(config_path),
                profile,
            )
            if profile_value is None:
                raise ValueError("DataCop API Key 为空，且现有 profile 配置不完整")
            if configured_value is not None and profile_value != configured_value:
                raise RuntimeError("各 Hermes profile 的 DataCop MCP 配置不一致")
            configured_value = profile_value

    if configured_value is None:
        raise ValueError("DataCop API Key 不能为空")
    configured_url, headers = configured_value
    if configured_url != expected_url:
        raise ValueError("更改 DataCop MCP 地址时必须输入 API Key")
    authorization = headers["Authorization"]
    if not isinstance(authorization, str):
        raise RuntimeError("DataCop MCP Authorization header 无效")
    return authorization.removeprefix("Bearer ")


def resolve_datacop_api_key(value: object, expected_url: str) -> str:
    """Validate a submitted key or reuse the matching stored credential."""

    if value == "":
        return _stored_datacop_api_key(expected_url)
    return validate_datacop_api_key(value)


def get_datacop_mcp_configuration() -> dict[str, object]:
    """Read the managed DataCop configuration without returning its API key."""

    configured_profiles: list[str] = []
    missing_profiles: list[str] = []
    configured_value: tuple[str, dict[str, object]] | None = None

    with profile_config_lock:
        for profile in DATACOP_MCP_PROFILES:
            profile_home = Path(get_hermes_home_for_profile(profile))
            if profile != "default" and not profile_home.is_dir():
                missing_profiles.append(profile)
                continue
            current = _load_datacop_profile_config(profile_home / "config.yaml")
            profile_value = _read_profile_datacop_server(current, profile)
            if profile_value is None:
                continue
            if configured_value is not None and profile_value != configured_value:
                raise RuntimeError("各 Hermes profile 的 DataCop MCP 配置不一致")
            configured_value = profile_value
            configured_profiles.append(profile)

    available_profile_count = len(DATACOP_MCP_PROFILES) - len(missing_profiles)
    configured = (
        configured_value is not None
        and len(configured_profiles) == available_profile_count
    )
    return {
        "configured": configured,
        "datacop_mcp_url": configured_value[0] if configured_value else "",
        "has_api_key": configured_value is not None,
        "configured_profiles": configured_profiles,
        "missing_profiles": missing_profiles,
    }


def apply_datacop_mcp_configuration(
    datacop_mcp_url: object,
    api_key: object,
) -> dict[str, object]:
    """Probe, persist, and activate DataCop MCP in managed gateways."""

    with _DATACOP_MCP_APPLY_LOCK:
        with mcp_config_transaction_lock():
            normalized_url = normalize_datacop_mcp_url(datacop_mcp_url)
            resolved_key = resolve_datacop_api_key(api_key, normalized_url)
            probe_result = check_datacop_mcp_connection(normalized_url, resolved_key)
            result = configure_datacop_mcp_profiles(normalized_url, resolved_key)
            configured_profiles = result.get("configured_profiles")
            if not isinstance(configured_profiles, list) or not all(
                isinstance(profile, str) for profile in configured_profiles
            ):
                raise RuntimeError(
                    "DataCop MCP profile update returned invalid configured_profiles"
                )
            try:
                gateway_restart = restart_managed_gateways(tuple(configured_profiles))
            except RuntimeError as exc:
                raise RuntimeError(
                    "DataCop MCP 配置已保存，但 Gateway 重启失败"
                ) from exc
            try:
                reset_webui_mcp_runtime()
            except RuntimeError as exc:
                raise RuntimeError(
                    "DataCop MCP 配置已保存且 Gateway 已重启，"
                    "但 WebUI MCP 运行时重置失败"
                ) from exc
            return {
                **result,
                "tool_count": probe_result["tool_count"],
                "reload_required": False,
                "gateway_restart": gateway_restart,
            }
