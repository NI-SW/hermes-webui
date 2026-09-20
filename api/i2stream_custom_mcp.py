"""Configure user-defined HTTP MCP servers in managed Hermes profiles."""

from __future__ import annotations

import copy
import os
import re
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


CUSTOM_MCP_PROFILES = DEFAULT_GATEWAY_PROFILES
CUSTOM_MCP_MAX_HEADERS = 64
CUSTOM_MCP_MAX_HEADER_NAME_LENGTH = 256
CUSTOM_MCP_MAX_HEADER_VALUE_LENGTH = 8192
_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_FORBIDDEN_HEADER_NAMES = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_CUSTOM_MCP_APPLY_LOCK = threading.Lock()


_PROTECTED_SERVER_NAMES = frozenset(
    {
        "datacop",
        "i2stream-knowledge-mcp",
        "i2up-rag-service-mcp",
    }
)
_PROTECTED_SERVER_PREFIXES = ("i2up-stream-mcp-",)


def is_protected_mcp_server(server_name: str) -> bool:
    if server_name in _PROTECTED_SERVER_NAMES:
        return True
    return any(server_name.startswith(prefix) for prefix in _PROTECTED_SERVER_PREFIXES)


def validate_server_name(value: object) -> str:
    if not isinstance(value, str) or not _SERVER_NAME_RE.fullmatch(value):
        raise ValueError(
            "MCP 服务名称必须为 1 至 64 位字母、数字、点、下划线或连字符，且以字母或数字开头"
        )
    return value


def ensure_server_name_available(server_name: str) -> None:
    if is_protected_mcp_server(server_name):
        raise ValueError(f"MCP 服务名称 {server_name!r} 是系统内置服务，请更换名称")
    for profile in CUSTOM_MCP_PROFILES:
        try:
            profile_home = Path(get_hermes_home_for_profile(profile))
        except Exception:
            continue
        if profile != "default" and not profile_home.is_dir():
            continue
        config_path = profile_home / "config.yaml"
        if not config_path.exists():
            continue
        current = _load_profile_config_strict(config_path)
        servers = current.get("mcp_servers")
        if isinstance(servers, dict) and server_name in servers:
            raise ValueError(f"MCP 服务名称 {server_name!r} 已存在，请更换名称")


def normalize_mcp_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("MCP 地址不能为空")
    if value != value.strip() or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("MCP 地址不能包含空白或控制字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP 地址格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("MCP 地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise ValueError("MCP 地址必须包含主机")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP 地址不能包含查询参数或片段")

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}{parsed.path}"


def validate_headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("headers 必须是 JSON 对象")
    if len(value) > CUSTOM_MCP_MAX_HEADERS:
        raise ValueError(f"headers 最多允许 {CUSTOM_MCP_MAX_HEADERS} 项")

    validated: dict[str, str] = {}
    normalized_names: set[str] = set()
    for name, header_value in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Header 名称必须是非空字符串")
        if len(name) > CUSTOM_MCP_MAX_HEADER_NAME_LENGTH or not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"Header 名称 {name!r} 格式无效")
        normalized_name = name.lower()
        if normalized_name in normalized_names:
            raise ValueError(f"Header 名称 {name!r} 重复")
        if normalized_name in _FORBIDDEN_HEADER_NAMES:
            raise ValueError(f"Header {name!r} 由 HTTP 客户端管理，不能手动设置")
        if not isinstance(header_value, str):
            raise ValueError(f"Header {name!r} 的值必须是字符串")
        if len(header_value) > CUSTOM_MCP_MAX_HEADER_VALUE_LENGTH:
            raise ValueError(f"Header {name!r} 的值过长")
        if any(unicodedata.category(character) == "Cc" for character in header_value):
            raise ValueError(f"Header {name!r} 的值不能包含控制字符")
        normalized_names.add(normalized_name)
        validated[name] = header_value
    return validated


def validate_custom_mcp_request(
    server_name: object,
    mcp_url: object,
    headers: object,
) -> tuple[str, str, dict[str, str]]:
    name = validate_server_name(server_name)
    url = normalize_mcp_url(mcp_url)
    return name, url, validate_headers(headers)


def _load_profile_config_strict(config_path: Path) -> dict:
    if not config_path.exists():
        return load_profile_config_raw(config_path)
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read Hermes config.yaml") from exc
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Hermes profile config 无法读取：{config_path}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Hermes profile config 无法解析：{config_path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Hermes profile config 必须是对象：{config_path}")
    return loaded


def _server_config(url: str, headers: dict[str, str]) -> dict[str, object]:
    config: dict[str, object] = {"url": url, "enabled": True}
    if headers:
        config["headers"] = copy.deepcopy(headers)
    return config


def probe_mcp_server(name: str, config: dict) -> list[tuple[str, str]]:
    from hermes_cli.mcp_config import _probe_single_server

    return _probe_single_server(name, config)


def check_custom_mcp_connection(
    server_name: object,
    mcp_url: object,
    headers: object,
) -> dict[str, object]:
    name, url, validated_headers = validate_custom_mcp_request(
        server_name, mcp_url, headers
    )
    with profile_config_lock:
        ensure_server_name_available(name)
    try:
        tools = probe_mcp_server(name, _server_config(url, validated_headers))
    except Exception:
        raise RuntimeError("自定义 MCP 连接或工具探测失败") from None
    if not tools:
        raise RuntimeError("自定义 MCP 没有返回可用工具")
    return {
        "server_name": name,
        "mcp_url": url,
        "has_headers": bool(validated_headers),
        "tool_count": len(tools),
    }


def _config_with_custom_server(
    config: object,
    server_name: str,
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
    if server_name in servers:
        raise ValueError(f"MCP 服务名称 {server_name!r} 已存在，请更换名称")
    servers[server_name] = copy.deepcopy(server_config)
    updated["mcp_servers"] = servers
    return updated


def _restore_configs(
    written_paths: list[Path],
    snapshots: dict[Path, tuple[str | None, int | None]],
) -> None:
    errors: list[str] = []
    for path in reversed(written_paths):
        original, original_mode = snapshots[path]
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                restore_profile_config_text(path, original)
                if original_mode is None:
                    raise RuntimeError(f"缺少原始权限：{path}")
                path.chmod(original_mode)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError("自定义 MCP 配置回滚失败：" + "; ".join(errors))


def configure_custom_mcp_profiles(
    server_name: object,
    mcp_url: object,
    headers: object,
) -> dict[str, object]:
    name, url, validated_headers = validate_custom_mcp_request(
        server_name, mcp_url, headers
    )
    server_config = _server_config(url, validated_headers)
    contains_sensitive_headers = bool(validated_headers)

    with profile_config_lock:
        pending: list[tuple[str, Path, dict]] = []
        missing_profiles: list[str] = []
        for profile in CUSTOM_MCP_PROFILES:
            profile_home = Path(get_hermes_home_for_profile(profile))
            if profile != "default" and not profile_home.is_dir():
                missing_profiles.append(profile)
                continue
            config_path = profile_home / "config.yaml"
            current = _load_profile_config_strict(config_path)
            pending.append(
                (
                    profile,
                    config_path,
                    _config_with_custom_server(current, name, server_config),
                )
            )

        snapshots: dict[Path, tuple[str | None, int | None]] = {}
        for _profile, path, _config in pending:
            snapshots[path] = (
                path.read_text(encoding="utf-8") if path.exists() else None,
                stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
            )
        written_paths: list[Path] = []
        try:
            for _profile, path, config in pending:
                original, _mode = snapshots[path]
                if original is None and contains_sensitive_headers:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    os.close(descriptor)
                written_paths.append(path)
                if original is not None and contains_sensitive_headers:
                    path.chmod(0o600)
                save_profile_config(path, config)
                if contains_sensitive_headers:
                    path.chmod(0o600)
        except Exception as exc:
            try:
                _restore_configs(written_paths, snapshots)
            except RuntimeError as rollback_exc:
                raise RuntimeError(
                    f"自定义 MCP 配置写入失败；{rollback_exc}"
                ) from exc
            raise RuntimeError("自定义 MCP 配置写入失败") from exc

    try:
        reload_active_config()
    except Exception as exc:
        raise RuntimeError("自定义 MCP 配置已写入，但 WebUI 配置重新加载失败") from exc

    return {
        "server_name": name,
        "mcp_url": url,
        "has_headers": contains_sensitive_headers,
        "configured_profiles": [profile for profile, _path, _config in pending],
        "missing_profiles": missing_profiles,
        "reload_required": True,
    }


def apply_custom_mcp_configuration(
    server_name: object,
    mcp_url: object,
    headers: object,
) -> dict[str, object]:
    with _CUSTOM_MCP_APPLY_LOCK:
        with mcp_config_transaction_lock():
            name, url, validated_headers = validate_custom_mcp_request(
                server_name, mcp_url, headers
            )
            probe_result = check_custom_mcp_connection(name, url, validated_headers)
            result = configure_custom_mcp_profiles(name, url, validated_headers)
            configured_profiles = result.get("configured_profiles")
            if not isinstance(configured_profiles, list) or not all(
                isinstance(profile, str) for profile in configured_profiles
            ):
                raise RuntimeError(
                    "自定义 MCP profile 更新返回了无效的 configured_profiles"
                )
            try:
                gateway_restart = restart_managed_gateways(tuple(configured_profiles))
            except RuntimeError as exc:
                raise RuntimeError("自定义 MCP 配置已保存但重启失败") from exc
            try:
                reset_webui_mcp_runtime()
            except RuntimeError as exc:
                raise RuntimeError(
                    "自定义 MCP 配置已保存且 Gateway 已重启，但 WebUI MCP 运行时重置失败"
                ) from exc
            return {
                **result,
                "tool_count": probe_result["tool_count"],
                "reload_required": False,
                "gateway_restart": gateway_restart,
            }


def list_custom_mcp_servers() -> list[dict[str, object]]:
    with profile_config_lock:
        servers_by_name: dict[str, dict[str, object]] = {}
        for profile in CUSTOM_MCP_PROFILES:
            try:
                profile_home = Path(get_hermes_home_for_profile(profile))
            except Exception:
                continue
            if profile != "default" and not profile_home.is_dir():
                continue
            config_path = profile_home / "config.yaml"
            if not config_path.exists():
                continue
            current = _load_profile_config_strict(config_path)
            servers = current.get("mcp_servers")
            if not isinstance(servers, dict):
                continue
            for name, conf in servers.items():
                if not isinstance(name, str) or is_protected_mcp_server(name):
                    continue
                if name in servers_by_name:
                    continue
                if isinstance(conf, dict):
                    url = str(conf.get("url") or "")
                    has_headers = bool(conf.get("headers"))
                    enabled = bool(conf.get("enabled", True))
                else:
                    url = ""
                    has_headers = False
                    enabled = True
                servers_by_name[name] = {
                    "server_name": name,
                    "mcp_url": url,
                    "has_headers": has_headers,
                    "enabled": enabled,
                }
        return [servers_by_name[name] for name in sorted(servers_by_name.keys())]


def _config_without_custom_server(
    config: object,
    server_name: str,
) -> tuple[dict, bool]:
    if not isinstance(config, dict):
        raise RuntimeError("Hermes profile config must be an object")
    updated = copy.deepcopy(config)
    servers = updated.get("mcp_servers")
    if not isinstance(servers, dict) or server_name not in servers:
        return updated, False
    del servers[server_name]
    updated["mcp_servers"] = servers
    return updated, True


def delete_custom_mcp_profiles(server_name: object) -> dict[str, object]:
    name = validate_server_name(server_name)
    if is_protected_mcp_server(name):
        raise ValueError(f"系统内置 MCP 服务 {name!r} 不允许删除")

    with profile_config_lock:
        pending: list[tuple[str, Path, dict]] = []
        missing_profiles: list[str] = []
        found = False
        for profile in CUSTOM_MCP_PROFILES:
            profile_home = Path(get_hermes_home_for_profile(profile))
            if profile != "default" and not profile_home.is_dir():
                missing_profiles.append(profile)
                continue
            config_path = profile_home / "config.yaml"
            if not config_path.exists():
                continue
            current = _load_profile_config_strict(config_path)
            updated, removed = _config_without_custom_server(current, name)
            if removed:
                found = True
                pending.append((profile, config_path, updated))

        if not found:
            raise ValueError(f"MCP 服务 {name!r} 不存在")

        snapshots: dict[Path, tuple[str | None, int | None]] = {}
        for _profile, path, _config in pending:
            snapshots[path] = (
                path.read_text(encoding="utf-8") if path.exists() else None,
                stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
            )
        written_paths: list[Path] = []
        try:
            for _profile, path, config in pending:
                original, mode = snapshots[path]
                written_paths.append(path)
                save_profile_config(path, config)
                if mode is not None:
                    path.chmod(mode)
        except Exception as exc:
            try:
                _restore_configs(written_paths, snapshots)
            except RuntimeError as rollback_exc:
                raise RuntimeError(
                    f"删除自定义 MCP 配置回滚失败；{rollback_exc}"
                ) from exc
            raise RuntimeError("删除自定义 MCP 配置写入失败") from exc

    try:
        reload_active_config()
    except Exception as exc:
        raise RuntimeError("自定义 MCP 配置已删除，但 WebUI 配置重新加载失败") from exc

    return {
        "server_name": name,
        "configured_profiles": [profile for profile, _path, _config in pending],
        "missing_profiles": missing_profiles,
        "reload_required": True,
    }


def remove_custom_mcp_configuration(server_name: object) -> dict[str, object]:
    with _CUSTOM_MCP_APPLY_LOCK:
        with mcp_config_transaction_lock():
            result = delete_custom_mcp_profiles(server_name)
            configured_profiles = result.get("configured_profiles")
            if not isinstance(configured_profiles, list) or not all(
                isinstance(profile, str) for profile in configured_profiles
            ):
                raise RuntimeError(
                    "自定义 MCP profile 删除返回了无效的 configured_profiles"
                )
            try:
                gateway_restart = restart_managed_gateways(tuple(configured_profiles))
            except RuntimeError as exc:
                raise RuntimeError("自定义 MCP 配置已删除但重启失败") from exc
            try:
                reset_webui_mcp_runtime()
            except RuntimeError as exc:
                raise RuntimeError(
                    "自定义 MCP 配置已删除且 Gateway 已重启，但 WebUI MCP 运行时重置失败"
                ) from exc
            return {
                **result,
                "reload_required": False,
                "gateway_restart": gateway_restart,
            }

