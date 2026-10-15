"""Restart the shared i2Stream Hermes Gateway without restarting the container."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from api.profiles import get_hermes_home_for_profile


@dataclass(frozen=True)
class GatewayTarget:
    profile: str
    port: int


# MCP 配置仍分别写入两个逻辑 profile；它们由同一个 default 进程服务。
DEFAULT_GATEWAY_PROFILES = ("default", "stream-qa")
SHARED_GATEWAY = GatewayTarget(profile="default", port=8642)
MANAGED_GATEWAYS = {profile: SHARED_GATEWAY for profile in DEFAULT_GATEWAY_PROFILES}
# Hermes may spend up to 180 seconds draining active runs before the
# supervisor can launch the replacement. Leave another 150 seconds for the
# replacement process to initialize and publish a healthy endpoint.
RESTART_TIMEOUT_SECONDS = 330.0
POLL_INTERVAL_SECONDS = 0.25
RESTART_LOCK_PATH = Path("/tmp/i2stream-gateway-restart.lock")
SUPERVISOR_READY_PATH = Path("/tmp/i2stream-gateway-supervised")


@contextmanager
def _restart_lock():
    with RESTART_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another managed gateway restart is already in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _running_gateway_pid(target: GatewayTarget) -> int | None:
    from gateway.status import get_running_pid

    profile_home = Path(get_hermes_home_for_profile(target.profile))
    return get_running_pid(
        pid_path=profile_home / "gateway.pid",
        cleanup_stale=False,
    )


def _supervisor_is_ready() -> bool:
    return SUPERVISOR_READY_PATH.is_file()


def _gateway_health_ready(target: GatewayTarget) -> bool:
    return _profile_health_ready(target.profile)


def _profile_health_ready(profile: str) -> bool:
    prefix = "" if profile == "default" else f"/p/{profile}"
    request = Request(f"http://127.0.0.1:{SHARED_GATEWAY.port}{prefix}/health", method="GET")
    try:
        with urlopen(request, timeout=1.0) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _wait_for_replacement(
    target: GatewayTarget,
    old_pid: int,
    deadline: float,
) -> int:
    while time.monotonic() < deadline:
        new_pid = _running_gateway_pid(target)
        if new_pid is not None and new_pid != old_pid and _gateway_health_ready(target):
            return new_pid
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"{target.profile} gateway did not restart and become healthy "
        f"on port {target.port} before the restart timeout"
    )


def _resolve_targets(profiles: tuple[str, ...]) -> tuple[GatewayTarget, ...]:
    if not profiles:
        raise ValueError("At least one managed gateway profile is required")
    if len(set(profiles)) != len(profiles):
        raise ValueError("Managed gateway profiles must not contain duplicates")

    unknown = [profile for profile in profiles if profile not in MANAGED_GATEWAYS]
    if unknown:
        raise ValueError(f"Unsupported managed gateway profiles: {', '.join(unknown)}")
    return tuple(dict.fromkeys(MANAGED_GATEWAYS[profile] for profile in profiles))


def restart_managed_gateways(
    profiles: tuple[str, ...] = DEFAULT_GATEWAY_PROFILES,
    *,
    timeout_seconds: float = RESTART_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Replace the shared process once, then verify each requested profile route.

    The container supervisor treats Hermes' planned-restart exit code 75 as a
    request to launch default again. A QA-only request also restarts this process.
    """

    targets = _resolve_targets(tuple(profiles))
    if timeout_seconds <= 0:
        raise ValueError("Gateway restart timeout must be positive")
    if not hasattr(signal, "SIGUSR1"):
        raise RuntimeError("Managed gateway restart requires SIGUSR1 support")
    if not _supervisor_is_ready():
        raise RuntimeError("Managed gateway supervisor is not ready")

    with _restart_lock():
        old_pids: dict[str, int] = {}
        for target in targets:
            pid = _running_gateway_pid(target)
            if pid is None:
                raise RuntimeError(f"{target.profile} gateway is not running")
            old_pids[target.profile] = pid

        for target in targets:
            pid = old_pids[target.profile]
            try:
                os.kill(pid, signal.SIGUSR1)
            except (ProcessLookupError, PermissionError, OSError) as exc:
                raise RuntimeError(
                    f"Failed to request restart for {target.profile} gateway PID {pid}: {exc}"
                ) from exc

        deadline = time.monotonic() + timeout_seconds
        new_pids: dict[str, int] = {}
        for target in targets:
            old_pid = old_pids[target.profile]
            new_pids[target.profile] = _wait_for_replacement(target, old_pid, deadline)

        pending_profiles = list(profiles)
        while pending_profiles and time.monotonic() < deadline:
            pending_profiles = [
                profile for profile in pending_profiles if not _profile_health_ready(profile)
            ]
            if pending_profiles:
                time.sleep(POLL_INTERVAL_SECONDS)
        if pending_profiles:
            raise RuntimeError(
                f"{', '.join(pending_profiles)} profile routes did not become healthy "
                "before the restart timeout"
            )

        restarted: list[dict[str, object]] = []
        for profile in profiles:
            target = MANAGED_GATEWAYS[profile]
            restarted.append(
                {
                    "profile": profile,
                    "old_pid": old_pids[target.profile],
                    "new_pid": new_pids[target.profile],
                    "port": target.port,
                }
            )

    return {"status": "completed", "profiles": restarted}


def reset_webui_mcp_runtime() -> None:
    """Drop process-local MCP connections so the next Agent turn rediscovers them."""

    try:
        from api.agent_compat import agent_attr

        shutdown_mcp_servers = agent_attr(
            "tools.mcp_tool", "shutdown_mcp_servers", "tools.mcp_tool_lifecycle"
        )
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Hermes WebUI MCP runtime is unavailable") from exc
    try:
        shutdown_mcp_servers()
    except Exception as exc:
        raise RuntimeError("Failed to reset Hermes WebUI MCP runtime") from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restart the shared Hermes Gateway inside the running container"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    restart_parser = subparsers.add_parser("restart", help="restart the shared Gateway")
    restart_parser.add_argument(
        "profiles",
        nargs="*",
        choices=tuple(MANAGED_GATEWAYS),
        help="profile routes to check after the shared restart; defaults to both profiles",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    profiles = tuple(args.profiles) if args.profiles else DEFAULT_GATEWAY_PROFILES
    try:
        result = restart_managed_gateways(profiles)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
