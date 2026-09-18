from __future__ import annotations

import json
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest


def _response_json(handler: MagicMock) -> dict:
    return json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))


def test_datacop_mcp_get_handler_returns_only_redacted_state(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "api.i2stream_datacop_mcp.get_datacop_mcp_configuration",
        lambda: {
            "configured": True,
            "datacop_mcp_url": "http://datacop:8301/mcp",
            "has_api_key": True,
            "configured_profiles": ["default", "stream-qa"],
            "missing_profiles": [],
        },
    )
    handler = MagicMock()

    routes._handle_i2stream_datacop_mcp_get(handler)

    payload = _response_json(handler)
    assert handler.send_response.call_args.args[0] == 200
    assert payload["mcp"]["has_api_key"] is True
    assert "api_key" not in payload["mcp"]
    assert "Authorization" not in json.dumps(payload)


def test_datacop_mcp_check_handler_passes_secret_only_to_probe(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    check = MagicMock(
        return_value={
            "datacop_mcp_url": "http://datacop:8301/mcp",
            "tool_count": 4,
        }
    )
    monkeypatch.setattr(
        "api.i2stream_datacop_mcp.check_datacop_mcp_connection",
        check,
    )
    handler = MagicMock()

    routes._handle_i2stream_datacop_mcp_check(
        handler,
        {"datacop_mcp_url": "http://datacop:8301/mcp", "api_key": "opaque-key"},
    )

    check.assert_called_once_with("http://datacop:8301/mcp", "opaque-key")
    payload = _response_json(handler)
    assert handler.send_response.call_args.args[0] == 200
    assert payload["mcp"]["tool_count"] == 4
    assert "opaque-key" not in json.dumps(payload)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"datacop_mcp_url": "http://datacop:8301/mcp"},
        {
            "datacop_mcp_url": "http://datacop:8301/mcp",
            "api_key": "opaque-key",
            "unexpected": True,
        },
    ],
)
def test_datacop_mcp_write_handlers_reject_incomplete_or_extra_fields(
    monkeypatch, body
):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    for handler_function in (
        routes._handle_i2stream_datacop_mcp_check,
        routes._handle_i2stream_datacop_mcp_update,
    ):
        handler = MagicMock()
        handler_function(handler, body)
        assert handler.send_response.call_args.args[0] == 400


@pytest.mark.parametrize(
    "handler_function,args",
    [
        ("_handle_i2stream_datacop_mcp_get", ()),
        ("_handle_i2stream_datacop_mcp_check", ({},)),
        ("_handle_i2stream_datacop_mcp_update", ({},)),
    ],
)
def test_datacop_mcp_handlers_require_webui_login_protection(
    monkeypatch, handler_function, args
):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = MagicMock()

    getattr(routes, handler_function)(handler, *args)

    payload = _response_json(handler)
    assert handler.send_response.call_args.args[0] == 503
    assert "登录保护" in payload["error"]


@pytest.mark.parametrize(
    "method,path,target_name",
    [
        ("GET", "/api/datacop-mcp", "_handle_i2stream_datacop_mcp_get"),
        ("POST", "/api/datacop-mcp/check", "_handle_i2stream_datacop_mcp_check"),
        ("PUT", "/api/datacop-mcp", "_handle_i2stream_datacop_mcp_update"),
    ],
)
def test_datacop_mcp_routes_dispatch_after_security_guards(
    monkeypatch, method, path, target_name
):
    from api import routes

    handler = MagicMock()
    body = {"datacop_mcp_url": "http://datacop:8301/mcp", "api_key": "opaque-key"}
    target = MagicMock(return_value=True)
    monkeypatch.setattr(
        "api.i2stream_console.handle_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(routes, target_name, target)
    if method in {"POST", "PUT"}:
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "read_body", lambda _handler: body)

    handled = getattr(routes, f"handle_{method.lower()}")(handler, urlparse(path))

    assert handled is True
    if method == "GET":
        target.assert_called_once_with(handler)
    else:
        target.assert_called_once_with(handler, body)
