"""Cross-process transaction lock for managed Hermes MCP configuration."""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path


MCP_CONFIG_LOCK_PATH = Path("/tmp/i2stream-mcp-config.lock")
MCP_CONFIG_LOCK_TIMEOUT_SECONDS = 30.0
MCP_CONFIG_LOCK_POLL_SECONDS = 0.25


@contextmanager
def mcp_config_transaction_lock(
    timeout_seconds: float = MCP_CONFIG_LOCK_TIMEOUT_SECONDS,
):
    """Serialize config writes and their matching Gateway restarts."""

    if timeout_seconds <= 0:
        raise ValueError("MCP config lock timeout must be positive")
    with MCP_CONFIG_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个 MCP 配置任务正在执行，请稍后重试") from None
                time.sleep(MCP_CONFIG_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
