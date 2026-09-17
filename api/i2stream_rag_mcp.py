"""Persist the i2Stream RAG MCP endpoint in the managed Hermes profiles."""

from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import urlsplit

from api.config import (
    _cfg_lock as profile_config_lock,
    _load_yaml_config_file_raw as load_profile_config_raw,
    _save_yaml_config_file as save_profile_config,
    _write_yaml_config_text as restore_profile_config_text,
    reload_config as reload_active_config,
)
from api.profiles import get_hermes_home_for_profile


RAG_MCP_SERVER_NAME = "i2up-rag-service-mcp"
RAG_MCP_PROFILES = ("default", "stream-qa")


def normalize_rag_mcp_url(value: object) -> str:
    """Return the canonical RAG MCP URL accepted by the i2Stream page."""

    if not isinstance(value, str):
        raise ValueError("RAG Service MCP 地址必须是字符串")
    raw = value
    if not raw:
        raise ValueError("RAG Service MCP 地址不能为空")
    if raw != raw.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw
    ):
        raise ValueError("RAG Service MCP 地址不能包含空白或控制字符")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("RAG Service MCP 地址格式无效") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("RAG Service MCP 地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname or port is None:
        raise ValueError("RAG Service MCP 地址必须包含主机和端口")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("RAG Service MCP 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("RAG Service MCP 地址不能包含查询参数或片段")
    if parsed.path.rstrip("/") != "/mcp":
        raise ValueError("RAG Service MCP 地址路径必须是 /mcp")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}/mcp"


def _profile_config_with_rag_server(config: object, url: str) -> dict:
    if not isinstance(config, dict):
        raise RuntimeError("Hermes profile config must be an object")
    updated = copy.deepcopy(config)
    servers = updated.get("mcp_servers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise RuntimeError("Hermes profile mcp_servers must be an object")
    servers[RAG_MCP_SERVER_NAME] = {"url": url, "enabled": True}
    updated["mcp_servers"] = servers
    return updated


def _restore_written_configs(
    written_paths: list[Path], snapshots: dict[Path, str | None]
) -> None:
    rollback_errors: list[str] = []
    for path in reversed(written_paths):
        original = snapshots[path]
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                restore_profile_config_text(path, original)
        except OSError as exc:
            rollback_errors.append(f"{path}: {exc}")
    if rollback_errors:
        raise RuntimeError(
            "Failed to roll back Hermes MCP profile config: "
            + "; ".join(rollback_errors)
        )


def configure_rag_mcp_profiles(rag_service_mcp_url: object) -> dict[str, object]:
    """Write the fixed RAG MCP server to default and stream-qa profiles.

    ``stream-qa`` is reported as missing instead of being created implicitly;
    profile creation remains owned by the container bootstrap.
    """

    normalized_url = normalize_rag_mcp_url(rag_service_mcp_url)
    with profile_config_lock:
        pending: list[tuple[str, Path, dict]] = []
        missing_profiles: list[str] = []
        for profile in RAG_MCP_PROFILES:
            profile_home = Path(get_hermes_home_for_profile(profile))
            if profile != "default" and not profile_home.is_dir():
                missing_profiles.append(profile)
                continue
            current = load_profile_config_raw(profile_home / "config.yaml")
            pending.append(
                (
                    profile,
                    profile_home / "config.yaml",
                    _profile_config_with_rag_server(current, normalized_url),
                )
            )

        snapshots: dict[Path, str | None] = {}
        for _profile, path, _config in pending:
            snapshots[path] = path.read_text(encoding="utf-8") if path.exists() else None

        written_paths: list[Path] = []
        try:
            for _profile, path, config in pending:
                save_profile_config(path, config)
                written_paths.append(path)
        except Exception as exc:
            try:
                _restore_written_configs(written_paths, snapshots)
            except RuntimeError as rollback_exc:
                raise RuntimeError(
                    f"Failed to update Hermes MCP config ({exc}); {rollback_exc}"
                ) from exc
            raise RuntimeError("Failed to update Hermes MCP config") from exc

    try:
        reload_active_config()
    except Exception as exc:
        raise RuntimeError(
            "Hermes MCP config was saved, but the active WebUI config could not be reloaded"
        ) from exc

    return {
        "rag_service_mcp_url": normalized_url,
        "configured_profiles": [profile for profile, _path, _config in pending],
        "missing_profiles": missing_profiles,
        "reload_required": True,
    }
