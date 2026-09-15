from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import SecretStr, ValidationError

os.environ.setdefault("VECTOR_SEARCH_HOST", "http://127.0.0.1:8900")
os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import auth
import main
from logmonitor_installer import (
    CHECK_NAMES,
    InstallerConfig,
    InstallerError,
    LogMonitorInstaller,
    SSHClient,
)
from models import LogMonitorInstallRequest, LogMonitorPreflightRequest


PASSWORD = "password with spaces"


def request_values(**overrides):
    values = {
        "target_ip": "192.168.10.20",
        "ssh_port": 2222,
        "ssh_username": "deploy",
        "ssh_password": PASSWORD,
        "STREAM_LOG_MONITOR": "1",
        "MCP_IADEBUG_USER": "root",
        "ACTIVE_HOME": "/root/ia",
        "STREAM_HOME": "/root/i2stream",
        "STREAM_DATA_HOME": "/var/iadata",
    }
    values.update(overrides)
    return values


class FakeSSH:
    def __init__(self, image_digest: str):
        self.image_digest = image_digest
        self.scripts: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.passwords: list[str] = []

    def run_script(self, target_ip, port, username, password, script, **_kwargs):
        self.passwords.append(password)
        self.scripts.append(script)
        if "emit()" in script:
            output = "\n".join(
                f"I2CHECK\t{name}\tpassed" for name in CHECK_NAMES[1:]
            ) + "\n"
        elif script.startswith("mktemp"):
            output = "/tmp/i2stream-logmonitor.ABC12345\n"
        elif script.startswith("sha256sum"):
            output = self.image_digest + "\n"
        else:
            output = ""
        return subprocess.CompletedProcess([], 0, output.encode(), b"")

    def upload(self, target_ip, port, username, password, local_path, remote_path):
        self.passwords.append(password)
        path = Path(local_path)
        self.uploads.append((path.name, remote_path, path.read_bytes()))


class FailingProcessSSH(FakeSSH):
    def run_script(self, target_ip, port, username, password, script, **kwargs):
        result = super().run_script(
            target_ip,
            port,
            username,
            password,
            script,
            **kwargs,
        )
        if "docker top mcp-server" in script:
            return subprocess.CompletedProcess([], 1, b"", b"")
        return result


class ImmediateThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class FakeRouteInstaller:
    def __init__(self):
        self.preflight_call = None
        self.install_call = None

    def preflight(self, payload, callback_host):
        self.preflight_call = (payload, callback_host)
        return {
            "preflight_id": "a" * 32,
            "target_ip": payload.target_ip,
            "agent_base_url": f"http://{callback_host}:8642",
            "agent_back_url": f"http://{callback_host}:50091",
            "checks": [{"name": "ssh_login", "status": "passed", "message": "ok"}],
            "expires_at": "2026-09-14T12:00:00Z",
        }

    def create_installation(self, payload, callback_host):
        self.install_call = (payload, callback_host)
        return {
            "job_id": "b" * 32,
            "status": "queued",
            "stage": "queued",
            "message": "安装任务已创建",
        }

    def get_job(self, job_id):
        return {
            "job_id": job_id,
            "target_ip": "192.168.10.20",
            "status": "running",
            "stage": "transferring",
            "message": "正在传输安装制品",
            "checks": [],
            "created_at": "2026-09-14T12:00:00Z",
            "updated_at": "2026-09-14T12:01:00Z",
        }


class LogMonitorInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.image = root / "i2up-stream-mcp.tar"
        self.image.write_bytes(b"node-image" * 1024)
        self.script = root / "start_stream_mcp.sh"
        self.script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.ssh = FakeSSH(self.digest)
        self.installer = LogMonitorInstaller(
            InstallerConfig(
                agent_public_host="",
                agent_base_url_port=8642,
                agent_back_port=50091,
                hermes_api_key=SecretStr("agent-key-abcdefghijklmnopqrstuvwxyz0123456789"),
                image_path=self.image,
                start_script_path=self.script,
                known_hosts_path=root / "ssh" / "known_hosts",
                preflight_ttl_seconds=300,
            ),
            ssh_client=self.ssh,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_request_contract_uses_node_env_names_and_redacts_password(self) -> None:
        request = LogMonitorPreflightRequest(**request_values())

        self.assertEqual(request.stream_log_monitor, "1")
        self.assertEqual(request.mcp_iadebug_user, "root")
        self.assertNotIn(PASSWORD, repr(request))
        dumped = request.model_dump(mode="json")
        self.assertNotIn(PASSWORD, repr(dumped["ssh_password"]))

        for invalid in (
            {"target_ip": "::1"},
            {"target_ip": "127.0.0.1"},
            {"ACTIVE_HOME": "/root/ia;id"},
            {"ACTIVE_HOME": "/root/../etc"},
            {"ACTIVE_HOME": "/root/ia/"},
            {"ssh_username": "root;id"},
            {"ssh_password": "password\x00suffix"},
            {"STREAM_LOG_MONITOR": 1},
            {"STREAM_LOG_MONITOR": "0"},
        ):
            with self.subTest(invalid=next(iter(invalid))):
                with self.assertRaises(ValidationError):
                    LogMonitorPreflightRequest(**request_values(**invalid))

    def test_explicit_agent_host_has_priority_over_browser_host(self) -> None:
        config = InstallerConfig(
            **{
                **self.installer.config.__dict__,
                "agent_public_host": "10.20.30.40",
            }
        )
        installer = LogMonitorInstaller(config, ssh_client=self.ssh)

        self.assertEqual(installer.resolve_callback_host("192.168.1.9"), "10.20.30.40")
        self.assertEqual(self.installer.resolve_callback_host("192.168.1.9"), "192.168.1.9")
        with self.assertRaises(InstallerError):
            self.installer.resolve_callback_host("localhost")

    def test_node_env_preserves_valid_api_key_characters(self) -> None:
        config = InstallerConfig(
            **{
                **self.installer.config.__dict__,
                "hermes_api_key": SecretStr("agent/key+with=base64-padding"),
            }
        )
        installer = LogMonitorInstaller(config, ssh_client=self.ssh)
        request = LogMonitorInstallRequest(
            **request_values(),
            preflight_id="a" * 32,
        )

        node_env = installer._node_env(request, "192.168.1.10")

        self.assertIn("AGENT_API_KEY=agent/key+with=base64-padding\n", node_env)

    def test_preflight_fails_before_ssh_when_artifact_is_missing(self) -> None:
        self.image.unlink()

        with self.assertRaises(InstallerError) as raised:
            self.installer.preflight(
                LogMonitorPreflightRequest(**request_values()),
                "192.168.1.10",
            )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.ssh.scripts, [])

    def test_installation_transfers_fixed_files_and_never_exposes_password(self) -> None:
        preflight_request = LogMonitorPreflightRequest(**request_values())
        preflight = self.installer.preflight(preflight_request, "192.168.1.10")
        self.assertTrue(all(check["status"] == "passed" for check in preflight["checks"]))
        install_request = LogMonitorInstallRequest(
            **request_values(),
            preflight_id=preflight["preflight_id"],
        )

        with patch("logmonitor_installer.threading.Thread", ImmediateThread):
            created = self.installer.create_installation(
                install_request,
                "192.168.1.10",
            )

        job = self.installer.get_job(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["stage"], "completed")
        self.assertNotIn(PASSWORD, repr(preflight))
        self.assertNotIn(PASSWORD, repr(created))
        self.assertNotIn(PASSWORD, repr(job))
        self.assertNotIn(PASSWORD, "\n".join(self.ssh.scripts))

        uploaded = {name: content for name, _remote, content in self.ssh.uploads}
        self.assertEqual(
            set(uploaded),
            {"i2up-stream-mcp.tar", "start_stream_mcp.sh", "node.env"},
        )
        node_env = uploaded["node.env"].decode("utf-8")
        self.assertIn("STREAM_LOG_MONITOR=1\n", node_env)
        self.assertIn("AGENT_BASE_URL=http://192.168.1.10:8642\n", node_env)
        self.assertIn("AGENT_BACK_URL=http://192.168.1.10:50091\n", node_env)
        self.assertIn("AGENT_API_KEY=", node_env)
        self.assertNotIn(PASSWORD, node_env)
        self.assertTrue(any("docker top mcp-server" in script for script in self.ssh.scripts))
        self.assertTrue(any(script.startswith("rm -rf -- /tmp/i2stream-logmonitor") for script in self.ssh.scripts))

    def test_preflight_is_bound_to_parameters_and_consumed_by_installation(self) -> None:
        preflight = self.installer.preflight(
            LogMonitorPreflightRequest(**request_values()),
            "192.168.1.10",
        )
        changed = LogMonitorInstallRequest(
            **request_values(STREAM_HOME="/different/path"),
            preflight_id=preflight["preflight_id"],
        )
        with self.assertRaises(InstallerError) as raised:
            self.installer.create_installation(changed, "192.168.1.10")
        self.assertEqual(raised.exception.status_code, 409)

        valid = LogMonitorInstallRequest(
            **request_values(),
            preflight_id=preflight["preflight_id"],
        )
        with patch("logmonitor_installer.threading.Thread"):
            self.installer.create_installation(valid, "192.168.1.10")
        with self.assertRaises(InstallerError) as reused:
            self.installer.create_installation(valid, "192.168.1.10")
        self.assertEqual(reused.exception.status_code, 409)

    def test_failed_process_check_removes_only_the_managed_container(self) -> None:
        failing_ssh = FailingProcessSSH(self.digest)
        installer = LogMonitorInstaller(self.installer.config, ssh_client=failing_ssh)
        preflight = installer.preflight(
            LogMonitorPreflightRequest(**request_values()),
            "192.168.1.10",
        )
        install_request = LogMonitorInstallRequest(
            **request_values(),
            preflight_id=preflight["preflight_id"],
        )

        with patch("logmonitor_installer.threading.Thread", ImmediateThread):
            created = installer.create_installation(install_request, "192.168.1.10")

        job = installer.get_job(created["job_id"])
        self.assertEqual(job["status"], "failed")
        cleanup = next(script for script in failing_ssh.scripts if "managed=$(docker inspect" in script)
        self.assertIn("com.info2soft.logmonitor.managed", cleanup)
        self.assertIn("docker rm -f mcp-server", cleanup)

    def test_scp_uses_uppercase_port_flag_and_password_only_in_environment(self) -> None:
        client = SSHClient(Path(self.temp_dir.name) / "ssh2" / "known_hosts")
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with patch("logmonitor_installer.subprocess.run", fake_run):
            client.upload(
                "192.168.10.20",
                2222,
                "deploy",
                PASSWORD,
                self.script,
                "/tmp/i2stream-logmonitor.ABC12345/start_stream_mcp.sh",
            )

        argv = captured["argv"]
        self.assertEqual(argv[0:5], ["sshpass", "-e", "scp", "-P", "2222"])
        self.assertNotIn(PASSWORD, repr(argv))
        self.assertEqual(captured["env"]["SSHPASS"], PASSWORD)

    def test_backend_install_auth_fails_closed_and_compares_bearer(self) -> None:
        token = "install-token-at-least-32-bytes-long"
        with patch.object(auth.settings, "i2stream_install_internal_token", SecretStr("")):
            with self.assertRaises(HTTPException) as missing:
                auth.require_install_auth(None)
            self.assertEqual(missing.exception.status_code, 503)

        with patch.object(
            auth.settings,
            "i2stream_install_internal_token",
            SecretStr(token),
        ):
            with self.assertRaises(HTTPException) as invalid:
                auth.require_install_auth("Bearer wrong-token")
            self.assertEqual(invalid.exception.status_code, 401)
            self.assertIsNone(auth.require_install_auth(f"Bearer {token}"))

    def test_logmonitor_validation_response_does_not_echo_password(self) -> None:
        password = "sensitive-password-that-must-not-be-returned"
        request = SimpleNamespace(
            url=SimpleNamespace(path="/api/logmonitor/preflight"),
        )
        validation_error = RequestValidationError(
            [
                {
                    "type": "string_too_long",
                    "loc": ("body", "ssh_password"),
                    "msg": "String should have at most 1024 characters",
                    "input": password,
                }
            ],
            body={"ssh_password": password},
        )

        response = asyncio.run(
            main.request_validation_error_handler(request, validation_error)
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(password, response.body.decode("utf-8"))
        self.assertEqual(
            json.loads(response.body),
            {"error": "LogMonitor 安装参数格式无效"},
        )


class LogMonitorRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_preserve_callback_host_and_return_stable_contract(self) -> None:
        fake = FakeRouteInstaller()
        async def run_inline(function, *args):
            return function(*args)

        preflight_request = LogMonitorPreflightRequest(**request_values())
        install_request = LogMonitorInstallRequest(
            **request_values(),
            preflight_id="a" * 32,
        )
        with (
            patch.object(main, "logmonitor_installer", fake),
            patch.object(main.asyncio, "to_thread", run_inline),
        ):
            preflight = await main.logmonitor_preflight(
                preflight_request,
                "192.168.34.65",
                None,
            )
            install = await main.create_logmonitor_installation(
                install_request,
                "192.168.34.65",
                None,
            )
            job = await main.logmonitor_installation("b" * 32, None)
            with self.assertRaises(HTTPException) as invalid_job:
                await main.logmonitor_installation("not-a-job", None)

        routes = {
            route.path: route
            for route in main.app.routes
            if "logmonitor" in route.path
        }
        self.assertEqual(set(routes), {
            "/api/logmonitor/preflight",
            "/api/logmonitor/installations",
            "/api/logmonitor/installations/{job_id}",
        })
        self.assertEqual(routes["/api/logmonitor/installations"].status_code, 202)
        self.assertEqual(invalid_job.exception.status_code, 422)
        self.assertEqual(fake.preflight_call[1], "192.168.34.65")
        self.assertEqual(fake.install_call[1], "192.168.34.65")
        self.assertEqual(fake.install_call[0].stream_home, "/root/i2stream")
        self.assertNotIn(PASSWORD, repr(preflight))
        self.assertNotIn(PASSWORD, repr(install))
        self.assertNotIn(PASSWORD, repr(job))


if __name__ == "__main__":
    unittest.main()
