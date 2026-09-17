from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import json

import pytest


def test_normalize_rag_mcp_url_accepts_explicit_http_port():
    from api.i2stream_rag_mcp import normalize_rag_mcp_url

    assert normalize_rag_mcp_url("http://192.168.34.65:8900/mcp/") == (
        "http://192.168.34.65:8900/mcp"
    )
    assert normalize_rag_mcp_url("HTTP://RAG.EXAMPLE.TEST:8900/mcp/") == (
        "http://rag.example.test:8900/mcp"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        " http://192.168.34.65:8900/mcp",
        "http://192.168.34.65:8900/mcp ",
        "http://192.168.34.65:8900/mcp\n",
        "ftp://192.168.34.65:8900/mcp",
        "http://192.168.34.65/mcp",
        "http://user:password@192.168.34.65:8900/mcp",
        "http://192.168.34.65:8900/other",
        "http://192.168.34.65:8900/mcp?token=secret",
        "http://192.168.34.65:8900/mcp#fragment",
    ],
)
def test_normalize_rag_mcp_url_rejects_unsafe_or_incomplete_values(value):
    from api.i2stream_rag_mcp import normalize_rag_mcp_url

    with pytest.raises(ValueError):
        normalize_rag_mcp_url(value)


def test_configure_rag_mcp_profiles_updates_default_and_stream_qa(monkeypatch, tmp_path):
    from api import i2stream_rag_mcp

    homes = {
        "default": tmp_path / "default",
        "stream-qa": tmp_path / "profiles" / "stream-qa",
    }
    homes["default"].mkdir(parents=True)
    homes["stream-qa"].mkdir(parents=True)
    configs = {
        homes["default"]: {
            "mcp_servers": {
                "other": {"url": "http://other:9000/mcp", "enabled": True},
                "i2up-rag-service-mcp": {
                    "command": "old-command",
                    "args": ["--old"],
                    "enabled": False,
                },
            }
        },
        homes["stream-qa"]: {"model": {"default": "qa-model"}},
    }
    saved = {}

    monkeypatch.setattr(
        i2stream_rag_mcp,
        "get_hermes_home_for_profile",
        lambda name: homes[name],
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "load_profile_config_raw",
        lambda path: configs[Path(path).parent],
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "save_profile_config",
        lambda path, config: saved.__setitem__(Path(path), config),
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "reload_active_config",
        lambda: None,
    )

    result = i2stream_rag_mcp.configure_rag_mcp_profiles(
        "http://192.168.34.65:8900/mcp"
    )

    assert result == {
        "rag_service_mcp_url": "http://192.168.34.65:8900/mcp",
        "configured_profiles": ["default", "stream-qa"],
        "missing_profiles": [],
        "reload_required": True,
    }
    assert set(saved) == {
        homes["default"] / "config.yaml",
        homes["stream-qa"] / "config.yaml",
    }
    default_servers = saved[homes["default"] / "config.yaml"]["mcp_servers"]
    assert default_servers["other"] == {
        "url": "http://other:9000/mcp",
        "enabled": True,
    }
    assert default_servers["i2up-rag-service-mcp"] == {
        "url": "http://192.168.34.65:8900/mcp",
        "enabled": True,
    }
    assert saved[homes["stream-qa"] / "config.yaml"] == {
        "model": {"default": "qa-model"},
        "mcp_servers": {
            "i2up-rag-service-mcp": {
                "url": "http://192.168.34.65:8900/mcp",
                "enabled": True,
            }
        },
    }


def test_configure_rag_mcp_profiles_preserves_environment_references(
    monkeypatch, tmp_path
):
    from api import i2stream_rag_mcp

    homes = {
        "default": tmp_path / "default",
        "stream-qa": tmp_path / "profiles" / "stream-qa",
    }
    for home in homes.values():
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "model:\n  api_key: ${OPENAI_API_KEY}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        i2stream_rag_mcp,
        "get_hermes_home_for_profile",
        lambda name: homes[name],
    )
    monkeypatch.setattr(i2stream_rag_mcp, "reload_active_config", lambda: None)

    i2stream_rag_mcp.configure_rag_mcp_profiles(
        "http://192.168.34.65:8900/mcp"
    )

    for home in homes.values():
        saved_text = (home / "config.yaml").read_text(encoding="utf-8")
        assert "${OPENAI_API_KEY}" in saved_text
        assert "expanded-secret-value" not in saved_text


def test_configure_rag_mcp_profiles_reports_missing_stream_qa(monkeypatch, tmp_path):
    from api import i2stream_rag_mcp

    default_home = tmp_path / "default"
    default_home.mkdir()
    stream_qa_home = tmp_path / "profiles" / "stream-qa"
    saved = {}

    monkeypatch.setattr(
        i2stream_rag_mcp,
        "get_hermes_home_for_profile",
        lambda name: default_home if name == "default" else stream_qa_home,
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "load_profile_config_raw",
        lambda _path: {},
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "save_profile_config",
        lambda path, config: saved.__setitem__(Path(path), config),
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "reload_active_config",
        lambda: None,
    )

    result = i2stream_rag_mcp.configure_rag_mcp_profiles(
        "http://192.168.34.65:8900/mcp"
    )

    assert result["configured_profiles"] == ["default"]
    assert result["missing_profiles"] == ["stream-qa"]
    assert set(saved) == {default_home / "config.yaml"}


def test_configure_rag_mcp_profiles_rolls_back_when_second_write_fails(
    monkeypatch, tmp_path
):
    from api import i2stream_rag_mcp

    homes = {
        "default": tmp_path / "default",
        "stream-qa": tmp_path / "profiles" / "stream-qa",
    }
    original_contents = {}
    for profile, home in homes.items():
        home.mkdir(parents=True)
        path = home / "config.yaml"
        original_contents[path] = f"profile: {profile}\n"
        path.write_text(original_contents[path], encoding="utf-8")

    monkeypatch.setattr(
        i2stream_rag_mcp,
        "get_hermes_home_for_profile",
        lambda name: homes[name],
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "load_profile_config_raw",
        lambda _path: {},
    )
    writes = 0

    def fail_second_write(path, _config):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("second write failed")
        Path(path).write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(i2stream_rag_mcp, "save_profile_config", fail_second_write)
    monkeypatch.setattr(i2stream_rag_mcp, "reload_active_config", lambda: None)

    with pytest.raises(RuntimeError, match="Failed to update Hermes MCP config"):
        i2stream_rag_mcp.configure_rag_mcp_profiles(
            "http://192.168.34.65:8900/mcp"
        )

    assert {
        path: path.read_text(encoding="utf-8") for path in original_contents
    } == original_contents


def test_configure_rag_mcp_profiles_rejects_invalid_existing_server_shape(
    monkeypatch, tmp_path
):
    from api import i2stream_rag_mcp

    default_home = tmp_path / "default"
    default_home.mkdir()

    monkeypatch.setattr(
        i2stream_rag_mcp,
        "get_hermes_home_for_profile",
        lambda _name: default_home,
    )
    monkeypatch.setattr(
        i2stream_rag_mcp,
        "load_profile_config_raw",
        lambda _path: {"mcp_servers": []},
    )

    with pytest.raises(RuntimeError, match="mcp_servers"):
        i2stream_rag_mcp.configure_rag_mcp_profiles(
            "http://192.168.34.65:8900/mcp"
        )


def test_rag_mcp_update_handler_returns_profile_and_reload_state(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "api.i2stream_rag_mcp.configure_rag_mcp_profiles",
        lambda url: {
            "rag_service_mcp_url": url,
            "configured_profiles": ["default", "stream-qa"],
            "missing_profiles": [],
            "reload_required": True,
        },
    )
    handler = MagicMock()

    routes._handle_i2stream_rag_mcp_update(
        handler,
        {"rag_service_mcp_url": "http://192.168.34.65:8900/mcp"},
    )

    payload = json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))
    assert handler.send_response.call_args.args[0] == 200
    assert payload == {
        "code": 0,
        "status": "success",
        "mcp": {
            "rag_service_mcp_url": "http://192.168.34.65:8900/mcp",
            "configured_profiles": ["default", "stream-qa"],
            "missing_profiles": [],
            "reload_required": True,
        },
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rag_service_mcp_url": "http://host:8900/mcp", "unexpected": True},
    ],
)
def test_rag_mcp_update_handler_rejects_missing_or_extra_fields(monkeypatch, body):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    handler = MagicMock()
    routes._handle_i2stream_rag_mcp_update(handler, body)

    assert handler.send_response.call_args.args[0] == 400


def test_rag_mcp_update_handler_requires_webui_login_protection(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = MagicMock()
    routes._handle_i2stream_rag_mcp_update(
        handler,
        {"rag_service_mcp_url": "http://192.168.34.65:8900/mcp"},
    )

    payload = json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))
    assert handler.send_response.call_args.args[0] == 503
    assert "登录保护" in payload["error"]


def test_put_route_dispatches_rag_mcp_configuration(monkeypatch):
    from api import routes

    body = {"rag_service_mcp_url": "http://192.168.34.65:8900/mcp"}
    handler = MagicMock()
    target = MagicMock(return_value=True)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr("api.i2stream_console.handle_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(routes, "_handle_i2stream_rag_mcp_update", target)

    assert routes.handle_put(handler, urlparse("/api/rag-service-mcp")) is True
    target.assert_called_once_with(handler, body)
