"""Configure an installed i2Stream node MCP for the default Hermes Agent."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

from api.config import (
    _cfg_lock as profile_config_lock,
    _load_yaml_config_file_raw as load_profile_config_raw,
    _save_yaml_config_file as save_profile_config,
    reload_config as reload_active_config,
)
from api.i2stream_gateway_restart import restart_managed_gateways
from api.i2stream_mcp_config_lock import mcp_config_transaction_lock
from api.profiles import get_hermes_home_for_profile


NODE_MCP_PORT = 8643
NODE_MCP_PROFILE = "default"
_NODE_MCP_APPLY_LOCK = threading.Lock()


@dataclass(frozen=True)
class NodeMCPTarget:
    name: str
    url: str


def node_mcp_target(target_ip: object) -> NodeMCPTarget:
    """Build the historical per-node MCP name and endpoint."""

    if not isinstance(target_ip, str):
        raise ValueError("节点 MCP 地址必须是 IPv4 地址")
    try:
        address = IPv4Address(target_ip)
    except ValueError:
        raise ValueError("节点 MCP 地址必须是 IPv4 地址") from None
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("节点 MCP 地址必须是非回环单播 IPv4")

    octets = str(address).split(".")
    return NodeMCPTarget(
        name=f"i2up-stream-mcp-{octets[2]}-{octets[3]}",
        url=f"http://{address}:{NODE_MCP_PORT}/mcp",
    )


def probe_mcp_server(name: str, config: dict) -> list[tuple[str, str]]:
    """Connect to one MCP server and return the tools it publishes."""

    from hermes_cli.mcp_config import _probe_single_server

    return _probe_single_server(name, config)


def _profile_config_with_node_server(
    config: object,
    target: NodeMCPTarget,
) -> dict:
    if not isinstance(config, dict):
        raise RuntimeError("Hermes profile config must be an object")
    updated = copy.deepcopy(config)
    servers = updated.get("mcp_servers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise RuntimeError("Hermes profile mcp_servers must be an object")

    existing = servers.get(target.name)
    if existing is not None:
        if not isinstance(existing, dict) or not isinstance(existing.get("url"), str):
            raise RuntimeError(f"节点 MCP {target.name} 的现有配置无效")
        if existing["url"] != target.url:
            raise RuntimeError(
                f"节点 MCP 名称冲突：{target.name} 已指向其它目标地址"
            )

    servers[target.name] = {"url": target.url, "enabled": True}
    updated["mcp_servers"] = servers
    return updated


def _load_node_profile_config(config_path: Path) -> dict:
    if not config_path.exists():
        return load_profile_config_raw(config_path)
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read Hermes config.yaml") from exc
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Hermes profile config.yaml 无法读取") from exc
    try:
        loaded = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise RuntimeError("Hermes profile config.yaml 无法解析") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError("Hermes profile config.yaml 必须是对象")
    return loaded


def configure_node_mcp_profile(target: NodeMCPTarget) -> dict[str, object]:
    """Persist one node MCP in the default Hermes profile."""

    profile_home = Path(get_hermes_home_for_profile(NODE_MCP_PROFILE))
    config_path = profile_home / "config.yaml"
    with profile_config_lock:
        current = _load_node_profile_config(config_path)
        updated = _profile_config_with_node_server(current, target)
        try:
            save_profile_config(config_path, updated)
        except Exception as exc:
            raise RuntimeError("节点 MCP 配置写入失败") from exc

    try:
        reload_active_config()
    except Exception as exc:
        raise RuntimeError("节点 MCP 配置已写入，但 WebUI 配置重新加载失败") from exc

    return {
        "server_name": target.name,
        "server_url": target.url,
        "configured_profiles": [NODE_MCP_PROFILE],
    }


def apply_node_mcp_configuration(target_ip: object) -> dict[str, object]:
    """Probe, persist, and activate a node MCP on the default Gateway."""

    with _NODE_MCP_APPLY_LOCK:
        with mcp_config_transaction_lock():
            target = node_mcp_target(target_ip)
            server_config = {"url": target.url, "enabled": True}
            try:
                tools = probe_mcp_server(target.name, server_config)
            except Exception as exc:
                raise RuntimeError("节点 MCP 连接或工具探测失败") from exc
            if not tools:
                raise RuntimeError("节点 MCP 没有返回可用工具")

            result = configure_node_mcp_profile(target)
            gateway_restart = restart_managed_gateways((NODE_MCP_PROFILE,))
            return {
                **result,
                "tool_count": len(tools),
                "gateway_restart": gateway_restart,
            }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure an installed i2Stream node MCP for Hermes Agent"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    configure_parser = subparsers.add_parser(
        "configure",
        help="probe, save, and activate a node MCP",
    )
    configure_parser.add_argument("target_ip", help="installed node IPv4 address")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = apply_node_mcp_configuration(args.target_ip)
    except (ValueError, RuntimeError) as exc:
        print(
            json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "completed", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
