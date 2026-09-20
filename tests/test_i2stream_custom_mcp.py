from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ["custom", "vendor.mcp_1", "A-b", "a" * 64])
def test_validate_custom_mcp_name_accepts_supported_names(name):
    from api.i2stream_custom_mcp import validate_server_name

    assert validate_server_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-bad",
        "bad name",
        "a" * 65,
        "i2stream-knowledge-mcp",
        "i2up-rag-service-mcp",
        "datacop",
        "i2up-stream-mcp-50-19",
        "i2up-console-mcp-demo",
    ],
)
def test_validate_custom_mcp_name_rejects_invalid_or_reserved_names(name):
    from api.i2stream_custom_mcp import validate_server_name

    with pytest.raises(ValueError):
        validate_server_name(name)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("http://MCP.TEST", "http://mcp.test"),
        ("https://MCP.TEST:8443/mcp", "https://mcp.test:8443/mcp"),
    ],
)
def test_normalize_custom_mcp_url(raw, normalized):
    from api.i2stream_custom_mcp import normalize_mcp_url

    assert normalize_mcp_url(raw) == normalized


@pytest.mark.parametrize(
    "url",
    [
        "",
        "mcp.test",
        "ftp://mcp.test",
        "http://user:pass@mcp.test",
        "http://mcp.test/mcp?token=x",
        "http://mcp.test/mcp#fragment",
        " http://mcp.test",
    ],
)
def test_normalize_custom_mcp_url_rejects_unsafe_values(url):
    from api.i2stream_custom_mcp import normalize_mcp_url

    with pytest.raises(ValueError):
        normalize_mcp_url(url)


def test_validate_headers_accepts_custom_auth_headers_without_mutating_input():
    from api.i2stream_custom_mcp import validate_headers

    submitted = {
        "Authorization": "Basic dXNlcjpwYXNz",
        "X-API-Key": "opaque-token",
        "X-Tenant": "team alpha",
    }

    validated = validate_headers(submitted)

    assert validated == submitted
    assert validated is not submitted


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        (None, "JSON 对象"),
        ([], "JSON 对象"),
        ({"": "value"}, "非空字符串"),
        ({"Bad Header": "value"}, "格式无效"),
        ({"Host": "mcp.test"}, "不能手动设置"),
        ({"content-length": "10"}, "不能手动设置"),
        ({"X-Test": 1}, "必须是字符串"),
        ({"X-Test": "line\nbreak"}, "控制字符"),
        ({"X-Test": "x" * 8193}, "值过长"),
        ({"X-Test": "one", "x-test": "two"}, "重复"),
    ],
)
def test_validate_headers_rejects_invalid_or_unsafe_values(headers, message):
    from api.i2stream_custom_mcp import validate_headers

    with pytest.raises(ValueError, match=message):
        validate_headers(headers)


def test_validate_headers_rejects_too_many_entries():
    from api.i2stream_custom_mcp import validate_headers

    with pytest.raises(ValueError, match="最多"):
        validate_headers({f"X-{index}": "value" for index in range(65)})


def test_validate_custom_mcp_request_returns_normalized_values():
    from api.i2stream_custom_mcp import validate_custom_mcp_request

    assert validate_custom_mcp_request(
        "custom", "HTTPS://MCP.TEST/mcp", {"Authorization": "Bearer token"}
    ) == (
        "custom",
        "https://mcp.test/mcp",
        {"Authorization": "Bearer token"},
    )


def test_check_custom_mcp_connection_probes_without_exposing_headers(monkeypatch):
    from api import i2stream_custom_mcp

    calls = []
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "probe_mcp_server",
        lambda name, config: calls.append((name, config)) or [("search", "Search")],
    )

    result = i2stream_custom_mcp.check_custom_mcp_connection(
        "custom",
        "http://mcp.test/api",
        {"Authorization": "Bearer opaque-token", "X-Tenant": "alpha"},
    )

    assert calls == [
        (
            "custom",
            {
                "url": "http://mcp.test/api",
                "headers": {
                    "Authorization": "Bearer opaque-token",
                    "X-Tenant": "alpha",
                },
                "enabled": True,
            },
        )
    ]
    assert result == {
        "server_name": "custom",
        "mcp_url": "http://mcp.test/api",
        "has_headers": True,
        "tool_count": 1,
    }
    assert "opaque-token" not in json.dumps(result)


def test_check_custom_mcp_connection_omits_empty_headers(monkeypatch):
    from api import i2stream_custom_mcp

    seen = {}
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "probe_mcp_server",
        lambda _name, config: seen.update(config) or [("tool", "Tool")],
    )

    result = i2stream_custom_mcp.check_custom_mcp_connection(
        "custom", "http://mcp.test", {}
    )

    assert "headers" not in seen
    assert result["has_headers"] is False


def test_check_custom_mcp_connection_hides_probe_details(monkeypatch):
    from api import i2stream_custom_mcp

    monkeypatch.setattr(
        i2stream_custom_mcp,
        "probe_mcp_server",
        lambda _name, _config: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )

    with pytest.raises(RuntimeError, match="连接或工具探测失败") as exc_info:
        i2stream_custom_mcp.check_custom_mcp_connection(
            "custom", "http://mcp.test", {"Authorization": "secret"}
        )
    assert "secret detail" not in str(exc_info.value)


def test_configure_custom_mcp_profiles_updates_both_profiles_and_secures_headers(
    monkeypatch, tmp_path
):
    from api import i2stream_custom_mcp

    homes = {"default": tmp_path / "default", "stream-qa": tmp_path / "stream-qa"}
    for home in homes.values():
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")
        (home / "config.yaml").chmod(0o644)
    monkeypatch.setattr(
        i2stream_custom_mcp, "get_hermes_home_for_profile", lambda profile: homes[profile]
    )
    monkeypatch.setattr(i2stream_custom_mcp, "reload_active_config", lambda: None)

    result = i2stream_custom_mcp.configure_custom_mcp_profiles(
        "custom",
        "http://mcp.test/mcp",
        {"Authorization": "Bearer opaque-token", "X-Tenant": "alpha"},
    )

    import yaml

    for home in homes.values():
        config_path = home / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["mcp_servers"]["custom"]["headers"] == {
            "Authorization": "Bearer opaque-token",
            "X-Tenant": "alpha",
        }
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert result["configured_profiles"] == ["default", "stream-qa"]
    assert result["has_headers"] is True
    assert "opaque-token" not in json.dumps(result)


def test_configure_custom_mcp_secures_existing_file_before_header_write(
    monkeypatch, tmp_path
):
    from api import i2stream_custom_mcp

    homes = {"default": tmp_path / "default", "stream-qa": tmp_path / "stream-qa"}
    originals = {}
    for profile, home in homes.items():
        home.mkdir()
        config_path = home / "config.yaml"
        originals[config_path] = f"profile: {profile}\n"
        config_path.write_text(originals[config_path], encoding="utf-8")
        config_path.chmod(0o644)
    monkeypatch.setattr(
        i2stream_custom_mcp, "get_hermes_home_for_profile", lambda profile: homes[profile]
    )
    real_save = i2stream_custom_mcp.save_profile_config
    mode_after_header_write = []

    def fail_after_header_write(path, config):
        real_save(path, config)
        mode_after_header_write.append(stat.S_IMODE(Path(path).stat().st_mode))
        raise OSError("interrupted after header write")

    monkeypatch.setattr(
        i2stream_custom_mcp, "save_profile_config", fail_after_header_write
    )

    with pytest.raises(RuntimeError, match="配置写入失败"):
        i2stream_custom_mcp.configure_custom_mcp_profiles(
            "custom", "http://mcp.test", {"X-API-Key": "opaque-token"}
        )

    assert mode_after_header_write == [0o600]
    for path, original in originals.items():
        assert path.read_text(encoding="utf-8") == original
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_configure_custom_mcp_profiles_does_not_overwrite_malformed_yaml(
    monkeypatch, tmp_path
):
    from api import i2stream_custom_mcp

    homes = {"default": tmp_path / "default", "stream-qa": tmp_path / "stream-qa"}
    for home in homes.values():
        home.mkdir()
        (home / "config.yaml").write_text("model: [unterminated\n", encoding="utf-8")
    monkeypatch.setattr(
        i2stream_custom_mcp, "get_hermes_home_for_profile", lambda profile: homes[profile]
    )

    with pytest.raises(RuntimeError, match="无法解析"):
        i2stream_custom_mcp.configure_custom_mcp_profiles(
            "custom", "http://mcp.test", {}
        )

    assert (homes["default"] / "config.yaml").read_text(encoding="utf-8") == (
        "model: [unterminated\n"
    )


def test_configure_custom_mcp_profiles_rolls_back_first_write(monkeypatch, tmp_path):
    from api import i2stream_custom_mcp

    homes = {"default": tmp_path / "default", "stream-qa": tmp_path / "stream-qa"}
    originals = {}
    for profile, home in homes.items():
        home.mkdir()
        originals[profile] = f"profile: {profile}\n"
        (home / "config.yaml").write_text(originals[profile], encoding="utf-8")
    monkeypatch.setattr(
        i2stream_custom_mcp, "get_hermes_home_for_profile", lambda profile: homes[profile]
    )
    real_save = i2stream_custom_mcp.save_profile_config
    calls = 0

    def fail_second(path, config):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        real_save(path, config)

    monkeypatch.setattr(i2stream_custom_mcp, "save_profile_config", fail_second)

    with pytest.raises(RuntimeError, match="配置写入失败"):
        i2stream_custom_mcp.configure_custom_mcp_profiles(
            "custom", "http://mcp.test", {}
        )

    for profile, home in homes.items():
        assert (home / "config.yaml").read_text(encoding="utf-8") == originals[profile]


def test_apply_custom_mcp_configuration_orders_transaction(monkeypatch):
    from api import i2stream_custom_mcp

    events = []

    @contextmanager
    def transaction_lock():
        events.append("lock-enter")
        yield
        events.append("lock-exit")

    headers = {"X-API-Key": "opaque-token"}
    monkeypatch.setattr(i2stream_custom_mcp, "mcp_config_transaction_lock", transaction_lock)
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "check_custom_mcp_connection",
        lambda *args: events.append(("probe", args))
        or {
            "server_name": "custom",
            "mcp_url": "http://mcp.test",
            "has_headers": True,
            "tool_count": 2,
        },
    )
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "configure_custom_mcp_profiles",
        lambda *args: events.append(("write", args))
        or {
            "server_name": "custom",
            "mcp_url": "http://mcp.test",
            "has_headers": True,
            "configured_profiles": ["default", "stream-qa"],
        },
    )
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "restart_managed_gateways",
        lambda profiles: events.append(("restart", profiles)) or {"status": "completed"},
    )
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "reset_webui_mcp_runtime",
        lambda: events.append("reset"),
    )

    result = i2stream_custom_mcp.apply_custom_mcp_configuration(
        "custom", "http://mcp.test", headers
    )

    assert events == [
        "lock-enter",
        ("probe", ("custom", "http://mcp.test", headers)),
        ("write", ("custom", "http://mcp.test", headers)),
        ("restart", ("default", "stream-qa")),
        "reset",
        "lock-exit",
    ]
    assert result["tool_count"] == 2
    assert result["gateway_restart"] == {"status": "completed"}


def test_apply_custom_mcp_reports_saved_configuration_when_restart_fails(monkeypatch):
    from api import i2stream_custom_mcp

    monkeypatch.setattr(
        i2stream_custom_mcp,
        "check_custom_mcp_connection",
        lambda *_args: {"tool_count": 1},
    )
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "configure_custom_mcp_profiles",
        lambda *_args: {"configured_profiles": ["default", "stream-qa"]},
    )
    monkeypatch.setattr(
        i2stream_custom_mcp,
        "restart_managed_gateways",
        lambda _profiles: (_ for _ in ()).throw(RuntimeError("stopped")),
    )

    with pytest.raises(RuntimeError, match="配置已保存但重启失败"):
        i2stream_custom_mcp.apply_custom_mcp_configuration(
            "custom", "http://mcp.test", {}
        )
