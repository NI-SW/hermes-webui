from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from pydantic import SecretStr

from models import LogMonitorInstallRequest, LogMonitorPreflightRequest


CALLBACK_HOST_HEADER = "X-I2Stream-Agent-Host"
CHECK_NAMES = (
    "ssh_login",
    "architecture",
    "docker",
    "transfer_tools",
    "active_home",
    "stream_home",
    "stream_data_home",
    "log_directory",
    "disk_space",
    "container_absent",
    "agent_base_url",
    "agent_back_url",
)
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
_REMOTE_TEMP_RE = re.compile(r"^/tmp/i2stream-logmonitor\.[A-Za-z0-9]+$")
_IMAGE_ARCHIVE_MEMBER = "stream_node_mcp/i2up-stream-mcp.tar"
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_RETAINED_JOBS = 200
_MAX_RETAINED_PREFLIGHTS = 1000
_PREFLIGHT_TIMEOUT_SECONDS = 60
logger = logging.getLogger(__name__)


class InstallerError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class InstallerConfig:
    agent_public_host: str
    agent_base_url_port: int
    agent_back_port: int
    hermes_api_key: SecretStr
    image_path: Path
    start_script_path: Path
    known_hosts_path: Path
    preflight_ttl_seconds: int


@dataclass(frozen=True)
class _PreflightRecord:
    binding: str
    expires_at: datetime
    checks: list[dict[str, str]]


class SSHClient:
    def __init__(self, known_hosts_path: Path, timeout_seconds: int = 20):
        self.known_hosts_path = known_hosts_path
        self.timeout_seconds = timeout_seconds

    def _prepare_known_hosts(self) -> None:
        self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts_path.parent.chmod(0o700)
        self.known_hosts_path.touch(mode=0o600, exist_ok=True)
        self.known_hosts_path.chmod(0o600)

    def _common_options(self) -> list[str]:
        return [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts_path}",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=10",
        ]

    @staticmethod
    def _environment(password: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment["SSHPASS"] = password
        return environment

    @staticmethod
    def _key_environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("SSHPASS", None)
        return environment

    @staticmethod
    def _write_private_key(directory: Path, private_key: str) -> Path:
        directory.chmod(0o700)
        key_path = directory / "identity"
        key_path.write_text(private_key, encoding="utf-8")
        key_path.chmod(0o600)
        return key_path

    def _run_script_with_key(
        self,
        target_ip: str,
        port: int,
        username: str,
        private_key: str,
        script: str,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="i2stream-ssh-key-") as temporary_dir:
            key_path = self._write_private_key(Path(temporary_dir), private_key)
            argv = [
                "ssh",
                "-p",
                str(port),
                *self._common_options(),
                "-i",
                str(key_path),
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "PasswordAuthentication=no",
                f"{username}@{target_ip}",
                "bash -s",
            ]
            return subprocess.run(
                argv,
                input=script.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._key_environment(),
                timeout=timeout_seconds,
                check=False,
            )

    def _run_script_with_password(
        self,
        target_ip: str,
        port: int,
        username: str,
        password: str,
        script: str,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [
            "sshpass",
            "-e",
            "ssh",
            "-p",
            str(port),
            *self._common_options(),
            "-o", "PasswordAuthentication=yes",
            "-o", "NumberOfPasswordPrompts=1",
            f"{username}@{target_ip}",
            "bash -s",
        ]
        return subprocess.run(
            argv,
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(password),
            timeout=timeout_seconds,
            check=False,
        )

    def _run_script_with_key_and_password(
        self,
        target_ip: str,
        port: int,
        username: str,
        password: str,
        private_key: str,
        script: str,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="i2stream-ssh-key-") as temporary_dir:
            key_path = self._write_private_key(Path(temporary_dir), private_key)
            argv = [
                "sshpass",
                "-e",
                "ssh",
                "-p",
                str(port),
                *self._common_options(),
                "-i",
                str(key_path),
                "-o", "IdentitiesOnly=yes",
                "-o", "PreferredAuthentications=publickey,password",
                "-o", "PasswordAuthentication=yes",
                "-o", "NumberOfPasswordPrompts=1",
                f"{username}@{target_ip}",
                "bash -s",
            ]
            return subprocess.run(
                argv,
                input=script.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment(password),
                timeout=timeout_seconds,
                check=False,
            )

    def run_script(
        self,
        target_ip: str,
        port: int,
        username: str,
        password: str | None,
        script: str,
        *,
        private_key: str | None = None,
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        self._prepare_known_hosts()
        timeout = timeout_seconds or self.timeout_seconds
        if private_key is not None and password is not None:
            return self._run_script_with_key_and_password(
                target_ip,
                port,
                username,
                password,
                private_key,
                script,
                timeout,
            )
        if private_key is not None:
            return self._run_script_with_key(
                target_ip,
                port,
                username,
                private_key,
                script,
                timeout,
            )
        if password is None:
            raise ValueError("SSH password or private key is required")
        return self._run_script_with_password(
            target_ip,
            port,
            username,
            password,
            script,
            timeout,
        )

    def _upload_with_key(
        self,
        target_ip: str,
        port: int,
        username: str,
        private_key: str,
        local_path: Path,
        remote_path: str,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="i2stream-ssh-key-") as temporary_dir:
            key_path = self._write_private_key(Path(temporary_dir), private_key)
            argv = [
                "scp",
                "-P",
                str(port),
                *self._common_options(),
                "-i",
                str(key_path),
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "PasswordAuthentication=no",
                str(local_path),
                f"{username}@{target_ip}:{remote_path}",
            ]
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._key_environment(),
                timeout=max(self.timeout_seconds, 600),
                check=False,
            )

    def _upload_with_password(
        self,
        target_ip: str,
        port: int,
        username: str,
        password: str,
        local_path: Path,
        remote_path: str,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [
            "sshpass", "-e", "scp", "-P", str(port), *self._common_options(),
            "-o", "PasswordAuthentication=yes",
            "-o", "NumberOfPasswordPrompts=1",
            str(local_path), f"{username}@{target_ip}:{remote_path}",
        ]
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(password),
            timeout=max(self.timeout_seconds, 600),
            check=False,
        )

    def _upload_with_key_and_password(
        self,
        target_ip: str,
        port: int,
        username: str,
        password: str,
        private_key: str,
        local_path: Path,
        remote_path: str,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="i2stream-ssh-key-") as temporary_dir:
            key_path = self._write_private_key(Path(temporary_dir), private_key)
            argv = [
                "sshpass", "-e", "scp", "-P", str(port),
                *self._common_options(),
                "-i", str(key_path),
                "-o", "IdentitiesOnly=yes",
                "-o", "PreferredAuthentications=publickey,password",
                "-o", "PasswordAuthentication=yes",
                "-o", "NumberOfPasswordPrompts=1",
                str(local_path), f"{username}@{target_ip}:{remote_path}",
            ]
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment(password),
                timeout=max(self.timeout_seconds, 600),
                check=False,
            )

    def upload(
        self,
        target_ip: str,
        port: int,
        username: str,
        password: str | None,
        local_path: Path,
        remote_path: str,
        *,
        private_key: str | None = None,
    ) -> None:
        self._prepare_known_hosts()
        if private_key is not None and password is not None:
            result = self._upload_with_key_and_password(
                target_ip,
                port,
                username,
                password,
                private_key,
                local_path,
                remote_path,
            )
        elif private_key is not None:
            result = self._upload_with_key(
                target_ip,
                port,
                username,
                private_key,
                local_path,
                remote_path,
            )
        elif password is not None:
            result = self._upload_with_password(
                target_ip,
                port,
                username,
                password,
                local_path,
                remote_path,
            )
        else:
            raise ValueError("SSH password or private key is required")
        if result.returncode != 0:
            raise InstallerError("文件传输失败", 502)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _canonical_callback_host(value: str) -> str:
    try:
        address = IPv4Address(value)
    except ValueError:
        raise InstallerError("Agent 回调地址必须是 IPv4 地址", 400) from None
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise InstallerError("Agent 回调地址必须是非回环单播 IPv4", 400)
    return str(address)


class LogMonitorInstaller:
    def __init__(
        self,
        config: InstallerConfig,
        *,
        ssh_client: SSHClient | None = None,
    ):
        self.config = config
        self.ssh = ssh_client or SSHClient(config.known_hosts_path)
        self._lock = threading.Lock()
        self._preflights: dict[str, _PreflightRecord] = {}
        self._jobs: dict[str, dict[str, object]] = {}
        self._active_targets: dict[str, str] = {}

    def resolve_callback_host(self, forwarded_host: str | None) -> str:
        value = self.config.agent_public_host or forwarded_host
        if not value:
            raise InstallerError("缺少 Agent 回调地址", 400)
        return _canonical_callback_host(value)

    def _urls(self, callback_host: str) -> tuple[str, str]:
        return (
            f"http://{callback_host}:{self.config.agent_base_url_port}",
            f"http://{callback_host}:{self.config.agent_back_port}",
        )

    @staticmethod
    def _binding(payload: LogMonitorPreflightRequest, callback_host: str) -> str:
        values = payload.model_dump(
            exclude={"ssh_password", "ssh_private_key", "preflight_id"},
            mode="json",
        )
        values["callback_host"] = callback_host
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _preflight_script(self, payload: LogMonitorPreflightRequest, callback_host: str) -> str:
        image_bytes = self._image_member_size()
        required_kb = max((image_bytes * 2 + 1023) // 1024, 1024)
        values = {
            "active_home": payload.active_home,
            "stream_home": payload.stream_home,
            "stream_data_home": payload.stream_data_home,
            "log_directory": f"{payload.stream_data_home}/log",
        }
        lines = [
            "emit() { printf 'I2CHECK\\t%s\\t%s\\n' \"$1\" \"$2\"; }",
            "case \"$(uname -m)\" in x86_64|amd64) emit architecture passed;; *) emit architecture failed;; esac",
            "if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then emit docker passed; else emit docker failed; fi",
            "if command -v sha256sum >/dev/null 2>&1 && command -v mktemp >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then emit transfer_tools passed; else emit transfer_tools failed; fi",
        ]
        for name, path in values.items():
            lines.append(f"if [ -d {shlex.quote(path)} ]; then emit {name} passed; else emit {name} failed; fi")
        lines.extend(
            [
                "available=$(df -Pk /tmp 2>/dev/null | awk 'NR==2 {print $4}')",
                f"if [ -n \"$available\" ] && [ \"$available\" -ge {required_kb} ] 2>/dev/null; then emit disk_space passed; else emit disk_space failed; fi",
                "if ! docker container inspect mcp-server >/dev/null 2>&1; then emit container_absent passed; else emit container_absent failed; fi",
                f"if timeout 5 bash -c '</dev/tcp/{callback_host}/{self.config.agent_base_url_port}' >/dev/null 2>&1; then emit agent_base_url passed; else emit agent_base_url failed; fi",
                f"if timeout 5 bash -c '</dev/tcp/{callback_host}/{self.config.agent_back_port}' >/dev/null 2>&1; then emit agent_back_url passed; else emit agent_back_url failed; fi",
            ]
        )
        return "\n".join(lines) + "\n"

    def _run_preflight_checks(
        self,
        payload: LogMonitorPreflightRequest,
        callback_host: str,
    ) -> list[dict[str, str]]:
        password = (
            payload.ssh_password.get_secret_value()
            if payload.ssh_password is not None
            else None
        )
        private_key = (
            payload.ssh_private_key.get_secret_value()
            if payload.ssh_private_key is not None
            else None
        )
        try:
            result = self.ssh.run_script(
                payload.target_ip,
                payload.ssh_port,
                payload.ssh_username,
                password,
                self._preflight_script(payload, callback_host),
                private_key=private_key,
                timeout_seconds=_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None

        statuses: dict[str, str] = {}
        if result is not None and result.returncode == 0:
            for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
                parts = raw_line.split("\t")
                if len(parts) == 3 and parts[0] == "I2CHECK" and parts[1] in CHECK_NAMES:
                    if parts[2] in {"passed", "failed"}:
                        statuses[parts[1]] = parts[2]

        messages = {
            "ssh_login": ("SSH 登录成功", "SSH 登录失败"),
            "architecture": ("目标架构为 amd64", "目标架构不是 amd64"),
            "docker": ("Docker 可用", "Docker 不可用或当前用户无权限"),
            "transfer_tools": ("安装所需命令可用", "缺少 sha256sum、mktemp 或 timeout"),
            "active_home": ("Active 目录存在", "Active 目录不存在"),
            "stream_home": ("Stream 目录存在", "Stream 目录不存在"),
            "stream_data_home": ("数据目录存在", "数据目录不存在"),
            "log_directory": ("日志目录存在", "日志目录不存在"),
            "disk_space": ("磁盘空间充足", "磁盘空间不足"),
            "container_absent": ("容器名可用", "mcp-server 容器已存在"),
            "agent_base_url": ("目标机可访问 Agent API", "目标机无法访问 Agent API"),
            "agent_back_url": ("目标机可访问 Agent 后端", "目标机无法访问 Agent 后端"),
        }
        login_ok = result is not None and result.returncode == 0
        checks = [{"name": "ssh_login", "status": "passed" if login_ok else "failed", "message": messages["ssh_login"][0 if login_ok else 1]}]
        for name in CHECK_NAMES[1:]:
            status = statuses.get(name, "failed") if login_ok else "failed"
            checks.append({"name": name, "status": status, "message": messages[name][0 if status == "passed" else 1]})
        return checks

    def preflight(self, payload: LogMonitorPreflightRequest, forwarded_host: str | None) -> dict[str, object]:
        callback_host = self.resolve_callback_host(forwarded_host)
        self._require_local_artifacts()
        checks = self._run_preflight_checks(payload, callback_host)
        preflight_id = uuid.uuid4().hex
        expires_at = _utc_now() + timedelta(seconds=self.config.preflight_ttl_seconds)
        record = _PreflightRecord(self._binding(payload, callback_host), expires_at, checks)
        with self._lock:
            now = _utc_now()
            self._preflights = {key: value for key, value in self._preflights.items() if value.expires_at > now}
            if len(self._preflights) >= _MAX_RETAINED_PREFLIGHTS:
                oldest = min(
                    self._preflights,
                    key=lambda key: self._preflights[key].expires_at,
                )
                self._preflights.pop(oldest, None)
            self._preflights[preflight_id] = record
        agent_base_url, agent_back_url = self._urls(callback_host)
        return {
            "preflight_id": preflight_id,
            "target_ip": payload.target_ip,
            "agent_base_url": agent_base_url,
            "agent_back_url": agent_back_url,
            "checks": checks,
            "expires_at": _timestamp(expires_at),
        }

    def create_installation(self, payload: LogMonitorInstallRequest, forwarded_host: str | None) -> dict[str, object]:
        callback_host = self.resolve_callback_host(forwarded_host)
        with self._lock:
            record = self._preflights.get(payload.preflight_id)
            if record is None or record.expires_at <= _utc_now():
                raise InstallerError("预检结果不存在或已过期", 409)
            if not secrets.compare_digest(record.binding, self._binding(payload, callback_host)):
                raise InstallerError("安装参数与预检参数不一致", 409)
            if any(check["status"] != "passed" for check in record.checks):
                raise InstallerError("预检未通过", 409)
            active_job = self._active_targets.get(payload.target_ip)
            if active_job is not None and self._jobs[active_job]["status"] in ACTIVE_JOB_STATUSES:
                raise InstallerError("目标机器已有安装任务在执行", 409)
            self._prune_jobs_locked()
            if len(self._jobs) >= _MAX_RETAINED_JOBS:
                raise InstallerError("安装任务队列已满", 503)
            self._preflights.pop(payload.preflight_id, None)
            job_id = uuid.uuid4().hex
            now = _timestamp(_utc_now())
            self._jobs[job_id] = {
                "job_id": job_id,
                "target_ip": payload.target_ip,
                "status": "queued",
                "stage": "queued",
                "message": "安装任务已创建",
                "checks": [dict(check) for check in record.checks],
                "created_at": now,
                "updated_at": now,
            }
            self._active_targets[payload.target_ip] = job_id
        worker = threading.Thread(
            target=self._run_installation,
            args=(job_id, payload, callback_host),
            name=f"logmonitor-install-{job_id[:8]}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            self._finish_job(job_id, "failed", "无法启动安装任务")
            raise InstallerError("无法启动安装任务", 503) from None
        return {"job_id": job_id, "status": "queued", "stage": "queued", "message": "安装任务已创建"}

    def get_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise InstallerError("安装任务不存在", 404)
            return {**job, "checks": [dict(check) for check in job["checks"]]}

    def _update_job(self, job_id: str, stage: str, message: str, *, checks=None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["stage"] = stage
            job["message"] = message
            job["updated_at"] = _timestamp(_utc_now())
            if checks is not None:
                job["checks"] = [dict(check) for check in checks]

    def _finish_job(self, job_id: str, status: str, message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = status
            job["stage"] = status
            job["message"] = message
            job["updated_at"] = _timestamp(_utc_now())
            self._active_targets.pop(str(job["target_ip"]), None)

    def _prune_jobs_locked(self) -> None:
        if len(self._jobs) < _MAX_RETAINED_JOBS:
            return
        terminal = sorted(
            (
                (str(job["updated_at"]), job_id)
                for job_id, job in self._jobs.items()
                if job["status"] not in ACTIVE_JOB_STATUSES
            )
        )
        for _, job_id in terminal[: max(1, len(self._jobs) - _MAX_RETAINED_JOBS + 1)]:
            self._jobs.pop(job_id, None)

    def _require_local_artifacts(self) -> None:
        for path in (self.config.image_path, self.config.start_script_path):
            try:
                valid = path.is_file() and path.stat().st_size > 0 and os.access(path, os.R_OK)
            except OSError:
                valid = False
            if not valid:
                raise InstallerError("LogMonitor 安装制品缺失或不可读", 503)
        self._image_member_size()

    @staticmethod
    def _image_member(archive: tarfile.TarFile) -> tarfile.TarInfo:
        matches = [
            member
            for member in archive.getmembers()
            if member.name == _IMAGE_ARCHIVE_MEMBER
        ]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size <= 0:
            raise InstallerError("LogMonitor 镜像交付包无效或缺少镜像文件", 503)
        return matches[0]

    def _image_member_size(self) -> int:
        try:
            with tarfile.open(self.config.image_path, "r:gz") as archive:
                return self._image_member(archive).size
        except InstallerError:
            raise
        except (OSError, tarfile.TarError):
            raise InstallerError("LogMonitor 镜像交付包无效或缺少镜像文件", 503) from None

    def _extract_image(self, destination_dir: Path) -> Path:
        destination = destination_dir / "i2up-stream-mcp.tar"
        try:
            with tarfile.open(self.config.image_path, "r:gz") as archive:
                member = self._image_member(archive)
                source = archive.extractfile(member)
                if source is None:
                    raise InstallerError("LogMonitor 镜像交付包无效或缺少镜像文件", 503)
                with source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
        except InstallerError:
            raise
        except (OSError, tarfile.TarError):
            raise InstallerError("LogMonitor 镜像交付包无效或缺少镜像文件", 503) from None
        return destination

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
        return digest.hexdigest()

    def _node_env(self, payload: LogMonitorInstallRequest, callback_host: str) -> str:
        agent_base_url, agent_back_url = self._urls(callback_host)
        values = {
            "STREAM_LOG_MONITOR": str(payload.stream_log_monitor),
            "MCP_IADEBUG_USER": payload.mcp_iadebug_user,
            "ACTIVE_HOME": payload.active_home,
            "STREAM_HOME": payload.stream_home,
            "STREAM_DATA_HOME": payload.stream_data_home,
            "AGENT_BACK_URL": agent_back_url,
            "AGENT_BASE_URL": agent_base_url,
            "AGENT_API_KEY": self.config.hermes_api_key.get_secret_value(),
        }
        if any(
            not value or any(character in value for character in "\x00\r\n")
            for value in values.values()
        ):
            raise InstallerError("服务端环境变量无法写入 node.env", 503)
        return "".join(f"{name}={value}\n" for name, value in values.items())

    def _run_required(self, payload: LogMonitorInstallRequest, script: str, *, timeout: int = 60) -> bytes:
        password = (
            payload.ssh_password.get_secret_value()
            if payload.ssh_password is not None
            else None
        )
        private_key = (
            payload.ssh_private_key.get_secret_value()
            if payload.ssh_private_key is not None
            else None
        )
        result = self.ssh.run_script(
            payload.target_ip,
            payload.ssh_port,
            payload.ssh_username,
            password,
            script,
            private_key=private_key,
            timeout_seconds=timeout,
        )
        if result.returncode != 0:
            raise InstallerError("目标机器执行安装命令失败", 502)
        return result.stdout

    def _remove_failed_container(self, payload: LogMonitorInstallRequest) -> bool:
        script = (
            "managed=$(docker inspect -f '{{ index .Config.Labels \"com.info2soft.logmonitor.managed\" }}' "
            "mcp-server 2>/dev/null || true)\n"
            "if [ \"$managed\" = true ]; then docker rm -f mcp-server >/dev/null; fi\n"
        )
        try:
            self._run_required(payload, script)
        except (InstallerError, OSError, subprocess.TimeoutExpired):
            return False
        return True

    def _finish_failed_installation(
        self,
        job_id: str,
        payload: LogMonitorInstallRequest,
        message: str,
        container_start_attempted: bool,
    ) -> None:
        if container_start_attempted and not self._remove_failed_container(payload):
            message += "；自动清理失败，请检查目标机上的 mcp-server 容器"
        self._finish_job(job_id, "failed", message)

    def _run_installation(self, job_id: str, payload: LogMonitorInstallRequest, callback_host: str) -> None:
        remote_dir: str | None = None
        container_start_attempted = False
        try:
            self._require_local_artifacts()
            self._update_job(job_id, "preflight", "正在重新检查目标机器")
            checks = self._run_preflight_checks(payload, callback_host)
            self._update_job(job_id, "preflight", "目标机器检查完成", checks=checks)
            if any(check["status"] != "passed" for check in checks):
                raise InstallerError("安装前检查未通过", 409)
            output = self._run_required(payload, "mktemp -d /tmp/i2stream-logmonitor.XXXXXXXX\n")
            remote_dir = output.decode("utf-8", errors="replace").strip()
            if not _REMOTE_TEMP_RE.fullmatch(remote_dir):
                raise InstallerError("目标机器未返回有效临时目录", 502)

            self._update_job(job_id, "transferring", "正在传输安装制品")
            with tempfile.TemporaryDirectory(prefix="i2stream-logmonitor-") as local_dir:
                local_path = Path(local_dir)
                image_path = self._extract_image(local_path)
                local_digest = self._sha256_file(image_path)
                env_path = local_path / "node.env"
                env_path.write_text(self._node_env(payload, callback_host), encoding="utf-8")
                env_path.chmod(0o600)
                transfers = (
                    (image_path, f"{remote_dir}/i2up-stream-mcp.tar"),
                    (self.config.start_script_path, f"{remote_dir}/start_stream_mcp.sh"),
                    (env_path, f"{remote_dir}/node.env"),
                )
                password = (
                    payload.ssh_password.get_secret_value()
                    if payload.ssh_password is not None
                    else None
                )
                private_key = (
                    payload.ssh_private_key.get_secret_value()
                    if payload.ssh_private_key is not None
                    else None
                )
                for local_path, remote_path in transfers:
                    self.ssh.upload(
                        payload.target_ip,
                        payload.ssh_port,
                        payload.ssh_username,
                        password,
                        local_path,
                        remote_path,
                        private_key=private_key,
                    )

            self._run_required(payload, f"chmod 0600 {shlex.quote(remote_dir)}/node.env\nchmod 0700 {shlex.quote(remote_dir)}/start_stream_mcp.sh\n")
            self._update_job(job_id, "verifying_artifact", "正在校验镜像文件")
            digest_output = self._run_required(payload, f"sha256sum {shlex.quote(remote_dir)}/i2up-stream-mcp.tar | awk '{{print $1}}'\n")
            remote_digest = digest_output.decode("ascii", errors="ignore").strip()
            if not secrets.compare_digest(local_digest, remote_digest):
                raise InstallerError("镜像文件校验失败", 502)

            self._update_job(job_id, "creating_container", "正在加载镜像并启动容器")
            container_start_attempted = True
            self._run_required(payload, f"cd {shlex.quote(remote_dir)} && ./start_stream_mcp.sh\n", timeout=900)
            self._update_job(job_id, "verifying_container", "正在检查容器状态")
            self._run_required(
                payload,
                "test \"$(docker inspect -f '{{.State.Running}}' mcp-server)\" = true\n",
            )
            process_check = (
                "for ((attempt=0; attempt<15; attempt++)); do "
                "if docker top mcp-server -eo pid,args | grep -F 'debugtool/log_monitor/logmonitor9.py' >/dev/null 2>&1; "
                "then exit 0; fi; sleep 2; done; exit 1\n"
            )
            self._run_required(payload, process_check, timeout=40)
            with self._lock:
                job_checks = [dict(check) for check in self._jobs[job_id]["checks"]]
            job_checks.append(
                {
                    "name": "logmonitor_process",
                    "status": "passed",
                    "message": "LogMonitor 进程正在运行",
                }
            )
            self._update_job(job_id, "verifying_logmonitor", "LogMonitor 进程已启动", checks=job_checks)
            self._finish_job(job_id, "completed", "LogMonitor 安装完成")
        except InstallerError as exc:
            self._finish_failed_installation(
                job_id,
                payload,
                str(exc),
                container_start_attempted,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._finish_failed_installation(
                job_id,
                payload,
                "远程安装执行失败",
                container_start_attempted,
            )
        except Exception:
            logger.error("LogMonitor installation failed unexpectedly for job %s", job_id)
            self._finish_failed_installation(
                job_id,
                payload,
                "远程安装执行失败",
                container_start_attempted,
            )
        finally:
            if remote_dir is not None:
                try:
                    self._run_required(payload, f"rm -rf -- {shlex.quote(remote_dir)}\n")
                except (InstallerError, OSError, subprocess.TimeoutExpired):
                    logger.warning("Unable to remove LogMonitor temporary directory for job %s", job_id)
