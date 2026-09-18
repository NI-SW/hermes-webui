"""Helpers for restarting the active-profile Hermes gateway."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from api.profiles import (
    _PROFILE_ID_RE,
    _is_root_profile,
    get_active_hermes_home,
    get_active_profile_name,
    get_hermes_home_for_profile,
)

logger = logging.getLogger(__name__)

_GATEWAY_RESTART_LOCK = threading.Lock()


def _resolve_hermes_command() -> str:
    """Resolve the CLI path used for active-profile gateway restarts."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd

    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _consume_stream(stream) -> None:
    """Drain a subprocess stream to prevent stdout/stderr pipe deadlocks."""
    try:
        while stream and stream.read(4096):
            pass
    except Exception:
        pass


def _release_lock() -> None:
    try:
        _GATEWAY_RESTART_LOCK.release()
    except RuntimeError:
        # The lock may already have been released by another path.
        pass


def _restart_supervised_gateway(
    profile: str,
    *,
    quick_timeout_seconds: float,
) -> dict:
    """Start a managed restart and preserve the HTTP helper's quick return."""
    from api.i2stream_gateway_restart import restart_managed_gateways

    if not _GATEWAY_RESTART_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "Restart already in progress. Please wait a moment and try again.",
        }

    finished = threading.Event()
    outcome: dict[str, dict] = {}

    def _restart() -> None:
        try:
            managed_restart = restart_managed_gateways((profile,))
            outcome["result"] = {
                "status": "completed",
                "message": "Managed gateway restarted successfully",
                "managed_restart": managed_restart,
            }
        except (ValueError, RuntimeError) as exc:
            outcome["result"] = {"status": "failed", "message": str(exc)}
        except Exception as exc:
            logger.exception("Unexpected managed gateway restart failure")
            outcome["result"] = {
                "status": "failed",
                "message": f"Internal error running restart: {type(exc).__name__}: {exc}",
            }
        finally:
            _release_lock()
            finished.set()

    try:
        threading.Thread(target=_restart, daemon=True).start()
    except Exception as exc:
        _release_lock()
        logger.exception("Failed to start managed gateway restart worker")
        return {
            "status": "failed",
            "message": f"Internal error running restart: {type(exc).__name__}: {exc}",
        }

    if finished.wait(timeout=quick_timeout_seconds):
        return outcome["result"]
    return {
        "status": "in_progress",
        "message": "Gateway service restart initiated (in progress)",
    }


def _gateway_restart_profile_context(profile: str | None = None) -> tuple[Path, str | None]:
    """Return the HERMES_HOME and CLI profile arg for a gateway restart."""
    if profile is None:
        raw_profile = str(get_active_profile_name() or "default").strip()
        active_home = Path(get_active_hermes_home())
    else:
        raw_profile = str(profile or "")
        if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile):
            raise ValueError(f"Invalid profile for gateway restart: {profile!r}")
        active_home = Path(get_hermes_home_for_profile(raw_profile))

    if (
        raw_profile == "default"
        and active_home.name == "default"
        and active_home.parent.name == "profiles"
    ):
        return active_home, None
    if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile) or _is_root_profile(raw_profile):
        return active_home, "default"
    return active_home, raw_profile


def restart_active_profile_gateway(
    *,
    profile: str | None = None,
    quick_timeout_seconds: float = 2.0,
    background_wait_seconds: float = 240.0,
) -> dict:
    """Restart the active Gateway through the available lifecycle controller.

    Returns a short status dict with these values:
    - completed: command finished quickly and succeeded.
    - in_progress: command did not finish within ``quick_timeout_seconds``.
    - failed: command finished quickly with non-zero exit status.
    - busy: restart already in progress from another caller.
    """
    from api.i2stream_gateway_restart import (
        MANAGED_GATEWAYS,
        _supervisor_is_ready,
    )

    if _supervisor_is_ready():
        if profile is None:
            raw_profile = str(get_active_profile_name() or "default").strip()
        else:
            raw_profile = str(profile or "")
            if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile):
                return {
                    "status": "failed",
                    "message": f"Invalid profile for gateway restart: {profile!r}",
                }
        managed_profile = "default" if _is_root_profile(raw_profile) else raw_profile
        if managed_profile not in MANAGED_GATEWAYS:
            return {
                "status": "failed",
                "message": (
                    f"Gateway profile {managed_profile!r} is not managed by "
                    "the i2Stream container supervisor"
                ),
            }
        return _restart_supervised_gateway(
            managed_profile,
            quick_timeout_seconds=quick_timeout_seconds,
        )

    if not _GATEWAY_RESTART_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "Restart already in progress. Please wait a moment and try again.",
        }

    try:
        active_home, cli_profile = _gateway_restart_profile_context(profile)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(active_home)
        hermes_cmd = _resolve_hermes_command()
        cmd = [hermes_cmd]
        if cli_profile is not None:
            cmd.extend(["--profile", cli_profile])
        cmd.extend(["gateway", "restart"])

        if cli_profile is None:
            logger.info(
                "Restarting gateway service via CLI command: %s gateway restart (HERMES_HOME=%s)",
                hermes_cmd,
                active_home,
            )
        else:
            logger.info(
                "Restarting gateway service via CLI command: %s --profile %s gateway restart (HERMES_HOME=%s)",
                hermes_cmd,
                cli_profile,
                active_home,
            )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            stdout, stderr = proc.communicate(timeout=quick_timeout_seconds)
            _release_lock()
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            if proc.returncode == 0:
                logger.info("Gateway service restarted successfully: %s", stdout)
                return {
                    "status": "completed",
                    "message": "Gateway service restarted successfully",
                    "detail": stdout or stderr,
                }

            logger.error("Gateway service restart failed with code %s: %s", proc.returncode, stderr)
            return {
                "status": "failed",
                "message": f"Restart failed: {stderr or stdout}",
                "detail": stdout or stderr,
                "returncode": proc.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.info(
                "Gateway restart is taking longer than %.1fs (likely draining in-flight runs);"
                " continuing in background",
                quick_timeout_seconds,
            )

            threading.Thread(target=_consume_stream, args=(proc.stdout,), daemon=True).start()
            threading.Thread(target=_consume_stream, args=(proc.stderr,), daemon=True).start()

            def _wait_and_release() -> None:
                try:
                    proc.wait(timeout=background_wait_seconds)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "Gateway restart process timed out after %.1fs. Terminating process.",
                        background_wait_seconds,
                    )
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            try:
                                proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                logger.error(
                                    "Gateway restart process refused to die even after SIGKILL.",
                                )
                    except Exception:
                        logger.exception("Failed to terminate timed out gateway restart process.")
                finally:
                    _release_lock()

            threading.Thread(target=_wait_and_release, daemon=True).start()
            return {
                "status": "in_progress",
                "message": "Gateway service restart initiated (in progress)",
            }
    except Exception as exc:
        _release_lock()
        logger.exception("Failed to run gateway restart command")
        return {
            "status": "failed",
            "message": f"Internal error running restart: {type(exc).__name__}: {exc}",
        }
