from __future__ import annotations

import json
import signal
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest


def test_restart_managed_gateways_signals_all_profiles_before_waiting(monkeypatch):
    from api import i2stream_gateway_restart as gateway_restart

    old_pids = {"default": 101, "stream-qa": 202}
    new_pids = {"default": 303, "stream-qa": 404}
    events: list[tuple] = []

    monkeypatch.setattr(gateway_restart, "_restart_lock", nullcontext)
    monkeypatch.setattr(gateway_restart, "_supervisor_is_ready", lambda: True)
    monkeypatch.setattr(
        gateway_restart,
        "_running_gateway_pid",
        lambda target: old_pids[target.profile],
    )
    monkeypatch.setattr(
        gateway_restart.os,
        "kill",
        lambda pid, sig: events.append(("signal", pid, sig)),
    )

    def wait_for_replacement(target, old_pid, deadline):
        events.append(("wait", target.profile, old_pid))
        return new_pids[target.profile]

    monkeypatch.setattr(gateway_restart, "_wait_for_replacement", wait_for_replacement)

    result = gateway_restart.restart_managed_gateways(("default", "stream-qa"))

    assert events == [
        ("signal", 101, signal.SIGUSR1),
        ("signal", 202, signal.SIGUSR1),
        ("wait", "default", 101),
        ("wait", "stream-qa", 202),
    ]
    assert result == {
        "status": "completed",
        "profiles": [
            {"profile": "default", "old_pid": 101, "new_pid": 303, "port": 8642},
            {"profile": "stream-qa", "old_pid": 202, "new_pid": 404, "port": 8641},
        ],
    }


def test_restart_managed_gateways_validates_every_pid_before_signalling(monkeypatch):
    from api import i2stream_gateway_restart as gateway_restart

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(gateway_restart, "_restart_lock", nullcontext)
    monkeypatch.setattr(gateway_restart, "_supervisor_is_ready", lambda: True)
    monkeypatch.setattr(
        gateway_restart,
        "_running_gateway_pid",
        lambda target: 101 if target.profile == "default" else None,
    )
    monkeypatch.setattr(
        gateway_restart.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(RuntimeError, match="stream-qa.*not running"):
        gateway_restart.restart_managed_gateways(("default", "stream-qa"))

    assert signals == []


@pytest.mark.parametrize("profiles", [(), ("unknown",), ("default", "default")])
def test_restart_managed_gateways_rejects_invalid_profile_sets(profiles):
    from api.i2stream_gateway_restart import restart_managed_gateways

    with pytest.raises(ValueError):
        restart_managed_gateways(profiles)


def test_restart_managed_gateways_refuses_to_signal_without_supervisor(monkeypatch):
    from api import i2stream_gateway_restart as gateway_restart

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(gateway_restart, "_supervisor_is_ready", lambda: False)
    monkeypatch.setattr(
        gateway_restart.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(RuntimeError, match="supervisor is not ready"):
        gateway_restart.restart_managed_gateways(("default",))

    assert signals == []


def test_wait_for_replacement_requires_new_pid_and_healthy_endpoint(monkeypatch):
    from api import i2stream_gateway_restart as gateway_restart

    target = gateway_restart.MANAGED_GATEWAYS["default"]
    pids = iter((101, 303, 303))
    health = iter((False, True))
    monkeypatch.setattr(gateway_restart, "_running_gateway_pid", lambda _target: next(pids))
    monkeypatch.setattr(gateway_restart, "_gateway_health_ready", lambda _target: next(health))
    monkeypatch.setattr(gateway_restart.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(gateway_restart.time, "sleep", lambda _seconds: None)

    assert gateway_restart._wait_for_replacement(target, 101, 2.0) == 303


def test_cli_restart_without_profiles_targets_all_managed_gateways(monkeypatch, capsys):
    from api import i2stream_gateway_restart as gateway_restart

    restart = MagicMock(return_value={"status": "completed", "profiles": []})
    monkeypatch.setattr(gateway_restart, "restart_managed_gateways", restart)

    assert gateway_restart.main(["restart"]) == 0
    restart.assert_called_once_with(gateway_restart.DEFAULT_GATEWAY_PROFILES)
    assert json.loads(capsys.readouterr().out) == {"status": "completed", "profiles": []}
