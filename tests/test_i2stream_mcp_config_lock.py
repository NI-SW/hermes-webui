from __future__ import annotations

import fcntl

import pytest


def test_mcp_config_transaction_lock_times_out_before_entering(monkeypatch, tmp_path):
    from api import i2stream_mcp_config_lock

    times = iter((1.0, 1.5, 2.0))
    monkeypatch.setattr(
        i2stream_mcp_config_lock,
        "MCP_CONFIG_LOCK_PATH",
        tmp_path / "mcp-config.lock",
    )
    monkeypatch.setattr(
        i2stream_mcp_config_lock.time,
        "monotonic",
        lambda: next(times),
    )
    monkeypatch.setattr(i2stream_mcp_config_lock.time, "sleep", lambda _seconds: None)

    def lock_is_busy(_fd, operation):
        if operation & fcntl.LOCK_NB:
            raise BlockingIOError

    monkeypatch.setattr(
        i2stream_mcp_config_lock.fcntl,
        "flock",
        lock_is_busy,
    )

    entered = False
    with pytest.raises(RuntimeError, match="正在执行"):
        with i2stream_mcp_config_lock.mcp_config_transaction_lock(
            timeout_seconds=1.0
        ):
            entered = True

    assert entered is False
