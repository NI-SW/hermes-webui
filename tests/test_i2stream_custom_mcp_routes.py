from __future__ import annotations

import json
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest


BODY = {
    "server_name": "custom",
    "mcp_url": "http://mcp.test/mcp",
    "headers": {
        "Authorization": "Bearer opaque-token",
        "X-Tenant": "alpha",
    },
}


def _response_json(handler: MagicMock) -> dict:
    return json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))


@pytest.mark.parametrize(
    "handler_name",
    ["_handle_i2stream_custom_mcp_check", "_handle_i2stream_custom_mcp_update"],
)
def test_custom_mcp_handlers_require_webui_login_protection(monkeypatch, handler_name):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = MagicMock()

    getattr(routes, handler_name)(handler, BODY)

    payload = _response_json(handler)
    assert handler.send_response.call_args.args[0] == 503
    assert "登录保护" in payload["error"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {key: value for key, value in BODY.items() if key != "headers"},
        {**BODY, "extra": True},
    ],
)
def test_custom_mcp_handlers_reject_incomplete_or_extra_fields(monkeypatch, body):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    for handler_name in (
        "_handle_i2stream_custom_mcp_check",
        "_handle_i2stream_custom_mcp_update",
    ):
        handler = MagicMock()
        getattr(routes, handler_name)(handler, body)
        assert handler.send_response.call_args.args[0] == 400


def test_custom_mcp_check_returns_redacted_result(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    check = MagicMock(
        return_value={
            "server_name": "custom",
            "mcp_url": "http://mcp.test/mcp",
            "has_headers": True,
            "tool_count": 3,
        }
    )
    monkeypatch.setattr("api.i2stream_custom_mcp.check_custom_mcp_connection", check)
    handler = MagicMock()

    routes._handle_i2stream_custom_mcp_check(handler, BODY)

    check.assert_called_once_with(
        "custom",
        "http://mcp.test/mcp",
        {"Authorization": "Bearer opaque-token", "X-Tenant": "alpha"},
    )
    payload = _response_json(handler)
    assert payload == {
        "code": 0,
        "status": "success",
        "mcp": {
            "server_name": "custom",
            "mcp_url": "http://mcp.test/mcp",
            "has_headers": True,
            "tool_count": 3,
        },
    }
    assert "opaque-token" not in json.dumps(payload)


@pytest.mark.parametrize(
    "method,path,target_name",
    [
        ("POST", "/api/custom-mcp/check", "_handle_i2stream_custom_mcp_check"),
        ("PUT", "/api/custom-mcp", "_handle_i2stream_custom_mcp_update"),
    ],
)
def test_custom_mcp_routes_dispatch_after_security_guards(
    monkeypatch, method, path, target_name
):
    from api import routes

    handler = MagicMock()
    target = MagicMock(return_value=True)
    monkeypatch.setattr(
        "api.i2stream_console.handle_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: BODY)
    monkeypatch.setattr(routes, target_name, target)

    handled = getattr(routes, f"handle_{method.lower()}")(handler, urlparse(path))

    assert handled is True
    target.assert_called_once_with(handler, BODY)
