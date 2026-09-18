from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest


def test_node_mcp_target_uses_historical_ip_suffix():
    from api.i2stream_node_mcp import node_mcp_target

    target = node_mcp_target("192.168.50.19")

    assert target.name == "i2up-stream-mcp-50-19"
    assert target.url == "http://192.168.50.19:8643/mcp"


@pytest.mark.parametrize(
    "value",
    ["", "localhost", "127.0.0.1", "0.0.0.0", "224.0.0.1", "::1"],
)
def test_node_mcp_target_rejects_invalid_or_non_unicast_ipv4(value):
    from api.i2stream_node_mcp import node_mcp_target

    with pytest.raises(ValueError):
        node_mcp_target(value)


def test_apply_node_mcp_configuration_probes_saves_and_restarts_default(
    monkeypatch,
    tmp_path,
):
    from api import i2stream_node_mcp

    default_home = tmp_path / "default"
    default_home.mkdir()
    existing = {
        "model": {"default": "test-model"},
        "mcp_servers": {
            "other": {"url": "http://other:9000/mcp", "enabled": True}
        },
    }
    events: list[tuple] = []
    saved: dict[Path, dict] = {}

    @contextmanager
    def transaction_lock():
        events.append(("lock_enter",))
        yield
        events.append(("lock_exit",))

    monkeypatch.setattr(
        i2stream_node_mcp,
        "get_hermes_home_for_profile",
        lambda profile: default_home,
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "load_profile_config_raw",
        lambda path: existing,
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "probe_mcp_server",
        lambda name, config: events.append(("probe", name, config))
        or [("get_log", "Read monitored logs")],
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "save_profile_config",
        lambda path, config: (
            events.append(("save", Path(path))),
            saved.__setitem__(Path(path), config),
        ),
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "reload_active_config",
        lambda: events.append(("reload",)),
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "restart_managed_gateways",
        lambda profiles: events.append(("restart", profiles))
        or {"status": "completed", "profiles": []},
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "mcp_config_transaction_lock",
        transaction_lock,
    )

    result = i2stream_node_mcp.apply_node_mcp_configuration("192.168.50.19")

    assert events == [
        ("lock_enter",),
        (
            "probe",
            "i2up-stream-mcp-50-19",
            {"url": "http://192.168.50.19:8643/mcp", "enabled": True},
        ),
        ("save", default_home / "config.yaml"),
        ("reload",),
        ("restart", ("default",)),
        ("lock_exit",),
    ]
    assert saved[default_home / "config.yaml"] == {
        "model": {"default": "test-model"},
        "mcp_servers": {
            "other": {"url": "http://other:9000/mcp", "enabled": True},
            "i2up-stream-mcp-50-19": {
                "url": "http://192.168.50.19:8643/mcp",
                "enabled": True,
            },
        },
    }
    assert result == {
        "server_name": "i2up-stream-mcp-50-19",
        "server_url": "http://192.168.50.19:8643/mcp",
        "configured_profiles": ["default"],
        "tool_count": 1,
        "gateway_restart": {"status": "completed", "profiles": []},
    }


def test_apply_node_mcp_configuration_requires_discovered_tools(monkeypatch):
    from api import i2stream_node_mcp

    monkeypatch.setattr(i2stream_node_mcp, "probe_mcp_server", lambda *_args: [])

    with pytest.raises(RuntimeError, match="没有返回可用工具"):
        i2stream_node_mcp.apply_node_mcp_configuration("192.168.50.19")


def test_configure_node_mcp_profile_rejects_name_collision(monkeypatch, tmp_path):
    from api import i2stream_node_mcp

    default_home = tmp_path / "default"
    default_home.mkdir()
    monkeypatch.setattr(
        i2stream_node_mcp,
        "get_hermes_home_for_profile",
        lambda _profile: default_home,
    )
    monkeypatch.setattr(
        i2stream_node_mcp,
        "load_profile_config_raw",
        lambda _path: {
            "mcp_servers": {
                "i2up-stream-mcp-50-19": {
                    "url": "http://10.10.50.19:8643/mcp",
                    "enabled": True,
                }
            }
        },
    )

    with pytest.raises(RuntimeError, match="名称冲突"):
        i2stream_node_mcp.configure_node_mcp_profile(
            i2stream_node_mcp.node_mcp_target("192.168.50.19")
        )


def test_configure_node_mcp_profile_rejects_malformed_yaml_without_overwrite(
    monkeypatch,
    tmp_path,
):
    from api import i2stream_node_mcp

    default_home = tmp_path / "default"
    default_home.mkdir()
    config_path = default_home / "config.yaml"
    original = "model: [unterminated\n"
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        i2stream_node_mcp,
        "get_hermes_home_for_profile",
        lambda _profile: default_home,
    )

    with pytest.raises(RuntimeError, match="无法解析"):
        i2stream_node_mcp.configure_node_mcp_profile(
            i2stream_node_mcp.node_mcp_target("192.168.50.19")
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_cli_returns_json_and_nonzero_on_configuration_failure(monkeypatch, capsys):
    from api import i2stream_node_mcp

    monkeypatch.setattr(
        i2stream_node_mcp,
        "apply_node_mcp_configuration",
        lambda _target_ip: (_ for _ in ()).throw(RuntimeError("gateway unavailable")),
    )

    assert i2stream_node_mcp.main(["configure", "192.168.50.19"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload == {"status": "failed", "error": "gateway unavailable"}
