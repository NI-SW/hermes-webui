from contextlib import contextmanager
from pathlib import Path

import json
import pytest


def test_normalize_datacop_mcp_url_accepts_only_canonical_mcp_endpoint():
    from api.i2stream_datacop_mcp import normalize_datacop_mcp_url

    assert normalize_datacop_mcp_url("HTTP://DATACOP.EXAMPLE.TEST:8301/mcp/") == (
        "http://datacop.example.test:8301/mcp"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " http://datacop:8301/mcp",
        "http://datacop:8301/mcp\n",
        "ftp://datacop:8301/mcp",
        "http://datacop/mcp",
        "http://user:secret@datacop:8301/mcp",
        "http://datacop:8301/other",
        "http://datacop:8301/mcp?key=secret",
        "http://datacop:8301/mcp#fragment",
    ],
)
def test_normalize_datacop_mcp_url_rejects_unsafe_or_incomplete_values(value):
    from api.i2stream_datacop_mcp import normalize_datacop_mcp_url

    with pytest.raises(ValueError):
        normalize_datacop_mcp_url(value)


@pytest.mark.parametrize(
    "value",
    [None, "", " key", "key ", "key with space", "key\nline", "key\x00value"],
)
def test_validate_datacop_api_key_rejects_missing_whitespace_and_controls(value):
    from api.i2stream_datacop_mcp import validate_datacop_api_key

    with pytest.raises(ValueError):
        validate_datacop_api_key(value)


def test_validate_datacop_api_key_accepts_opaque_key_and_enforces_length_limit():
    from api.i2stream_datacop_mcp import (
        DATACOP_API_KEY_MAX_LENGTH,
        validate_datacop_api_key,
    )

    opaque_key = "custom.key_-123"
    assert validate_datacop_api_key(opaque_key) == opaque_key
    with pytest.raises(ValueError, match="过长"):
        validate_datacop_api_key("x" * (DATACOP_API_KEY_MAX_LENGTH + 1))


def test_probe_datacop_mcp_uses_bearer_header_and_returns_no_key(monkeypatch):
    from api import i2stream_datacop_mcp

    calls = []

    def probe(name, config):
        calls.append((name, config))
        return [("search", "Search DataCop"), ("read", "Read DataCop")]

    monkeypatch.setattr(i2stream_datacop_mcp, "probe_mcp_server", probe)

    result = i2stream_datacop_mcp.check_datacop_mcp_connection(
        "http://datacop.example.test:8301/mcp", "opaque-key"
    )

    assert calls == [
        (
            "i2stream-knowledge-mcp",
            {
                "url": "http://datacop.example.test:8301/mcp",
                "headers": {
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer opaque-key",
                },
                "enabled": True,
            },
        )
    ]
    assert result == {
        "datacop_mcp_url": "http://datacop.example.test:8301/mcp",
        "tool_count": 2,
    }
    assert "opaque-key" not in json.dumps(result)


def test_probe_datacop_mcp_rejects_server_without_tools(monkeypatch):
    from api import i2stream_datacop_mcp

    monkeypatch.setattr(i2stream_datacop_mcp, "probe_mcp_server", lambda *_args: [])

    with pytest.raises(RuntimeError, match="没有返回可用工具"):
        i2stream_datacop_mcp.check_datacop_mcp_connection(
            "http://datacop:8301/mcp", "opaque-key"
        )


def test_probe_datacop_mcp_drops_secret_bearing_exception_context(monkeypatch):
    from api import i2stream_datacop_mcp

    def fail_probe(*_args):
        raise RuntimeError("request failed for Authorization: Bearer opaque-key")

    monkeypatch.setattr(i2stream_datacop_mcp, "probe_mcp_server", fail_probe)

    with pytest.raises(RuntimeError) as error:
        i2stream_datacop_mcp.check_datacop_mcp_connection(
            "http://datacop:8301/mcp", "opaque-key"
        )

    assert "opaque-key" not in str(error.value)
    assert error.value.__cause__ is None


def _profile_homes(tmp_path):
    homes = {
        "default": tmp_path / "default",
        "stream-qa": tmp_path / "profiles" / "stream-qa",
    }
    for home in homes.values():
        home.mkdir(parents=True)
    return homes


def test_configure_datacop_mcp_profiles_updates_both_profiles(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    configs = {
        homes["default"]: {
            "mcp_servers": {
                "other": {"url": "http://other:9000/mcp", "enabled": True},
                "datacop": {"url": "http://legacy:8301/mcp", "enabled": True},
            }
        },
        homes["stream-qa"]: {
            "model": {"default": "qa-model"},
            "mcp_servers": {
                "datacop": {"url": "http://legacy:8301/mcp", "enabled": True}
            },
        },
    }
    saved = {}
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda path: configs[Path(path).parent],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "save_profile_config",
        lambda path, config: saved.__setitem__(Path(path), config),
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp, "_secure_profile_config", lambda _path: None
    )
    monkeypatch.setattr(i2stream_datacop_mcp, "reload_active_config", lambda: None)

    result = i2stream_datacop_mcp.configure_datacop_mcp_profiles(
        "http://datacop:8301/mcp", "opaque-key"
    )

    expected_server = {
        "url": "http://datacop:8301/mcp",
        "headers": {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer opaque-key",
        },
        "enabled": True,
    }
    assert result == {
        "datacop_mcp_url": "http://datacop:8301/mcp",
        "configured": True,
        "has_api_key": True,
        "configured_profiles": ["default", "stream-qa"],
        "missing_profiles": [],
        "reload_required": True,
    }
    assert saved[homes["default"] / "config.yaml"]["mcp_servers"] == {
        "other": {"url": "http://other:9000/mcp", "enabled": True},
        "i2stream-knowledge-mcp": expected_server,
    }
    assert saved[homes["stream-qa"] / "config.yaml"] == {
        "model": {"default": "qa-model"},
        "mcp_servers": {"i2stream-knowledge-mcp": expected_server},
    }
    assert "opaque-key" not in json.dumps(result)


def test_configure_datacop_mcp_profiles_restricts_config_permissions(
    monkeypatch, tmp_path
):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    for home in homes.values():
        config_path = home / "config.yaml"
        config_path.write_text("model:\n  default: test-model\n", encoding="utf-8")
        config_path.chmod(0o644)
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(i2stream_datacop_mcp, "reload_active_config", lambda: None)

    i2stream_datacop_mcp.configure_datacop_mcp_profiles(
        "http://datacop:8301/mcp", "opaque-key"
    )

    assert {
        (home / "config.yaml").stat().st_mode & 0o777 for home in homes.values()
    } == {0o600}


def test_configure_datacop_mcp_profiles_does_not_overwrite_malformed_yaml(
    monkeypatch, tmp_path
):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    malformed_path = homes["default"] / "config.yaml"
    malformed = "mcp_servers: [\n"
    malformed_path.write_text(malformed, encoding="utf-8")
    saves = []
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "save_profile_config",
        lambda path, config: saves.append((path, config)),
    )

    with pytest.raises(RuntimeError, match="无法解析"):
        i2stream_datacop_mcp.configure_datacop_mcp_profiles(
            "http://datacop:8301/mcp", "opaque-key"
        )

    assert saves == []
    assert malformed_path.read_text(encoding="utf-8") == malformed


def test_check_datacop_mcp_can_reuse_matching_stored_key(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    server = {
        "url": "http://datacop:8301/mcp",
        "headers": {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer stored-key",
        },
        "enabled": True,
    }
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda _path: {"mcp_servers": {"i2stream-knowledge-mcp": server}},
    )
    calls = []
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "probe_mcp_server",
        lambda name, config: calls.append((name, config)) or [("search", "Search")],
    )

    result = i2stream_datacop_mcp.check_datacop_mcp_connection(
        "http://datacop:8301/mcp", ""
    )

    assert result["tool_count"] == 1
    assert calls[0][1]["headers"]["Authorization"] == "Bearer stored-key"
    assert "stored-key" not in json.dumps(result)


def test_blank_datacop_key_cannot_be_reused_for_a_different_url(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    server = {
        "url": "http://datacop:8301/mcp",
        "headers": {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer stored-key",
        },
        "enabled": True,
    }
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda _path: {"mcp_servers": {"i2stream-knowledge-mcp": server}},
    )

    with pytest.raises(ValueError, match="更改.*必须输入"):
        i2stream_datacop_mcp.check_datacop_mcp_connection(
            "http://other-datacop:8301/mcp", ""
        )


def test_read_datacop_mcp_status_is_redacted(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    server = {
        "url": "http://datacop:8301/mcp",
        "headers": {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer super-secret-key",
        },
        "enabled": True,
    }
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda _path: {"mcp_servers": {"i2stream-knowledge-mcp": server}},
    )

    status = i2stream_datacop_mcp.get_datacop_mcp_configuration()

    assert status == {
        "datacop_mcp_url": "http://datacop:8301/mcp",
        "configured": True,
        "has_api_key": True,
        "configured_profiles": ["default", "stream-qa"],
        "missing_profiles": [],
    }
    assert "super-secret-key" not in json.dumps(status)
    assert "Authorization" not in json.dumps(status)


def test_read_datacop_mcp_status_reports_unconfigured_profiles(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda _path: {"mcp_servers": {}},
    )

    assert i2stream_datacop_mcp.get_datacop_mcp_configuration() == {
        "datacop_mcp_url": "",
        "configured": False,
        "has_api_key": False,
        "configured_profiles": [],
        "missing_profiles": [],
    }


def test_read_datacop_mcp_status_reports_partial_configuration(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    server = {
        "url": "http://datacop:8301/mcp",
        "headers": {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer opaque-key",
        },
        "enabled": True,
    }
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda path: (
            {"mcp_servers": {"i2stream-knowledge-mcp": server}}
            if Path(path).parent == homes["default"]
            else {"mcp_servers": {}}
        ),
    )

    assert i2stream_datacop_mcp.get_datacop_mcp_configuration() == {
        "datacop_mcp_url": "http://datacop:8301/mcp",
        "configured": False,
        "has_api_key": True,
        "configured_profiles": ["default"],
        "missing_profiles": [],
    }


def test_read_datacop_mcp_status_rejects_unmanaged_extra_header(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda _path: {
            "mcp_servers": {
                "i2stream-knowledge-mcp": {
                    "url": "http://datacop:8301/mcp",
                    "headers": {
                        "Accept": "application/json, text/event-stream",
                        "Authorization": "Bearer opaque-key",
                        "X-Unmanaged": "value",
                    },
                    "enabled": True,
                }
            }
        },
    )

    with pytest.raises(RuntimeError, match="headers"):
        i2stream_datacop_mcp.get_datacop_mcp_configuration()


@pytest.mark.parametrize(
    ("stream_qa_url", "stream_qa_key"),
    [
        ("http://datacop-b:8301/mcp", "shared-key"),
        ("http://datacop-a:8301/mcp", "second-key"),
    ],
)
def test_read_datacop_mcp_configuration_rejects_profile_mismatch(
    monkeypatch, tmp_path, stream_qa_url, stream_qa_key
):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    configs = {
        homes["default"]: {
            "mcp_servers": {
                "i2stream-knowledge-mcp": {
                    "url": "http://datacop-a:8301/mcp",
                    "headers": {
                        "Accept": "application/json, text/event-stream",
                        "Authorization": "Bearer shared-key",
                    },
                    "enabled": True,
                }
            }
        },
        homes["stream-qa"]: {
            "mcp_servers": {
                "i2stream-knowledge-mcp": {
                    "url": stream_qa_url,
                    "headers": {
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {stream_qa_key}",
                    },
                    "enabled": True,
                }
            }
        },
    }
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "load_profile_config_raw",
        lambda path: configs[Path(path).parent],
    )

    with pytest.raises(RuntimeError, match="不一致"):
        i2stream_datacop_mcp.get_datacop_mcp_configuration()


def test_configure_datacop_mcp_profiles_rolls_back_first_write(monkeypatch, tmp_path):
    from api import i2stream_datacop_mcp

    homes = _profile_homes(tmp_path)
    originals = {}
    for profile, home in homes.items():
        path = home / "config.yaml"
        originals[path] = f"profile: {profile}\n"
        path.write_text(originals[path], encoding="utf-8")
        path.chmod(0o644)

    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "get_hermes_home_for_profile",
        lambda profile: homes[profile],
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp, "load_profile_config_raw", lambda _path: {}
    )
    writes = 0

    def fail_second_write(path, _config):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("second write failed")
        Path(path).write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(i2stream_datacop_mcp, "save_profile_config", fail_second_write)
    monkeypatch.setattr(i2stream_datacop_mcp, "reload_active_config", lambda: None)

    with pytest.raises(RuntimeError, match="DataCop MCP 配置写入失败"):
        i2stream_datacop_mcp.configure_datacop_mcp_profiles(
            "http://datacop:8301/mcp", "opaque-key"
        )

    assert {path: path.read_text(encoding="utf-8") for path in originals} == originals
    assert {path.stat().st_mode & 0o777 for path in originals} == {0o644}


def test_apply_datacop_mcp_configuration_probes_writes_restarts_and_resets(
    monkeypatch,
):
    from api import i2stream_datacop_mcp

    events = []

    @contextmanager
    def transaction_lock():
        events.append(("lock_enter",))
        yield
        events.append(("lock_exit",))

    monkeypatch.setattr(
        i2stream_datacop_mcp, "mcp_config_transaction_lock", transaction_lock
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "check_datacop_mcp_connection",
        lambda url, key: (
            events.append(("probe", url, key))
            or {"datacop_mcp_url": url, "tool_count": 3}
        ),
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "configure_datacop_mcp_profiles",
        lambda url, key: (
            events.append(("configure", url, key))
            or {
                "datacop_mcp_url": url,
                "configured": True,
                "has_api_key": True,
                "configured_profiles": ["default", "stream-qa"],
                "missing_profiles": [],
                "reload_required": True,
            }
        ),
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "restart_managed_gateways",
        lambda profiles: (
            events.append(("restart", profiles))
            or {"status": "completed", "profiles": []}
        ),
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "reset_webui_mcp_runtime",
        lambda: events.append(("reset",)),
    )

    result = i2stream_datacop_mcp.apply_datacop_mcp_configuration(
        "http://datacop:8301/mcp", "opaque-key"
    )

    assert events == [
        ("lock_enter",),
        ("probe", "http://datacop:8301/mcp", "opaque-key"),
        ("configure", "http://datacop:8301/mcp", "opaque-key"),
        ("restart", ("default", "stream-qa")),
        ("reset",),
        ("lock_exit",),
    ]
    assert result["tool_count"] == 3
    assert result["reload_required"] is False
    assert result["gateway_restart"] == {"status": "completed", "profiles": []}
    assert "opaque-key" not in json.dumps(result)


def test_apply_datacop_mcp_reports_saved_config_when_gateway_restart_fails(
    monkeypatch,
):
    from api import i2stream_datacop_mcp

    @contextmanager
    def transaction_lock():
        yield

    monkeypatch.setattr(
        i2stream_datacop_mcp, "mcp_config_transaction_lock", transaction_lock
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "check_datacop_mcp_connection",
        lambda _url, _key: {"tool_count": 1},
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "configure_datacop_mcp_profiles",
        lambda _url, _key: {
            "datacop_mcp_url": "http://datacop:8301/mcp",
            "configured_profiles": ["default"],
            "missing_profiles": ["stream-qa"],
        },
    )
    monkeypatch.setattr(
        i2stream_datacop_mcp,
        "restart_managed_gateways",
        lambda _profiles: (_ for _ in ()).throw(RuntimeError("gateway timeout")),
    )

    with pytest.raises(RuntimeError, match="配置已保存.*Gateway 重启失败"):
        i2stream_datacop_mcp.apply_datacop_mcp_configuration(
            "http://datacop:8301/mcp", "opaque-key"
        )
