# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manage the existing dashboard CLI, its canonical pages, reads and auth.

No web server or projection query is implemented here. Startup's one-time token
is consumed from a pipe and stored through the canonical credential API.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from omnibase_core.cli.cli_user_config import user_config_path

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import read_local_tenant_identity
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_dashboard_process import (
    ModelSetupDashboardProcess,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


def _stamp(pid: int) -> str | None:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.strip() or None


class HandlerSetupDashboard:
    """Authenticated native dashboard lifecycle with owned-PID verification."""

    name = "dashboard"

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        store: LocalByokCredentialStore | None = None,
    ) -> None:
        self._home = onex_home or user_config_path().parent
        self._state = self._home / "dashboard.json"
        self._store = store or LocalByokCredentialStore()

    def _read(self) -> ModelSetupDashboardProcess | None:
        try:
            fd = os.open(self._state, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError("Invalid dashboard process metadata")
            return ModelSetupDashboardProcess.model_validate_json(source.read(65537))

    def _write(self, value: ModelSetupDashboardProcess) -> None:
        fd, name = tempfile.mkstemp(prefix=".dashboard-", dir=self._home)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as target:
                target.write(value.model_dump_json() + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._state)
        finally:
            temporary.unlink(missing_ok=True)

    def _ready(self, record: ModelSetupDashboardProcess) -> bool:
        identity = read_local_tenant_identity(db_path=self._store.db_path)
        if identity is None or str(identity.tenant_uuid) != record.tenant_id:
            return False
        if _stamp(record.pid) != record.process_stamp:
            return False
        token = asyncio.run(self._store.get_secret(record.credential_ref))
        if not token:
            return False
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = client.get(
                record.url + "/projections",
                headers={"authorization": "Bearer " + token},
            )
        return (
            response.status_code == 200
            and response.json().get("tenant") == record.tenant_id
        )

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            record = self._read()
            if record is not None and self._ready(record):
                return ModelSetupObservation(
                    step=self.name,
                    status="success",
                    evidence={
                        "url": record.url,
                        "pid": str(record.pid),
                        "credential_ref": record.credential_ref,
                        "tenant_id": record.tenant_id,
                    },
                )
            return ModelSetupObservation(
                step=self.name,
                status="unavailable",
                fix="Run onex setup --action repair --component dashboard to start the authenticated local dashboard.",
            )
        except (OSError, ValueError, httpx.HTTPError, subprocess.SubprocessError):
            return ModelSetupObservation(
                step=self.name,
                status="failure",
                fix="Check the dashboard process record, pinned bundle and credential-store access before retrying.",
            )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if plan.action != "verify":
            self.apply(plan)
        return self.observe(plan)

    def _stop_record(self, record: ModelSetupDashboardProcess) -> None:
        if _stamp(record.pid) != record.process_stamp:
            return
        os.kill(record.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while (
            time.monotonic() < deadline and _stamp(record.pid) == record.process_stamp
        ):
            time.sleep(0.05)
        if _stamp(record.pid) == record.process_stamp:
            raise RuntimeError(
                "The owned dashboard has not stopped; no replacement was started"
            )

    def stop(self) -> None:
        record = self._read()
        if record is not None:
            self._stop_record(record)

    def apply(self, plan: ModelSetupPlan) -> None:
        self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            self._home / "dashboard.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            existing = self._read()
            if existing is not None:
                try:
                    if self._ready(existing):
                        return
                except httpx.HTTPError:
                    pass
                self._stop_record(existing)
            identity = read_local_tenant_identity(db_path=self._store.db_path)
            if identity is None:
                raise ValueError("Run onex local init before starting this dashboard")
            environment = os.environ.copy()
            for name in tuple(environment):
                if (
                    name == "OMNI_HOME"
                    or name.startswith(("OMNIBASE_", "BIFROST_", "KAFKA_", "POSTGRES_"))
                    or name
                    in (
                        "ONEX_CONTRACTS_DIR",
                        "ONEX_CONFIG_DIR",
                        "PYTHONPATH",
                        "ONEX_TENANT_ID",
                        "ONEX_TENANT_SLUG",
                        "DELEGATION_ROUTING_OVERLAY_PATH",
                        "DELEGATION_ROUTING_TIERS_PATH",
                    )
                ):
                    environment.pop(name)
            process = subprocess.Popen(
                [str(Path(sys.executable).parent / "onex"), "dashboard"],
                cwd=Path.home(),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                assert process.stdout is not None
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(
                            "The dashboard did not start; check its pinned bundle and local identity"
                        )
                    readable, _, _ = select.select([process.stdout], [], [], 1)
                    if not readable:
                        continue
                    line = process.stdout.readline(4097)
                    if len(line) > 4096:
                        raise ValueError("Invalid dashboard startup output")
                    prefix = b"onex dashboard: "
                    if not line.startswith(prefix):
                        continue
                    start = urlsplit(
                        line[len(prefix) :].split(b" ", 1)[0].decode().strip()
                    )
                    token = parse_qs(start.query).get("token", [""])[0]
                    stamp = _stamp(process.pid)
                    if (
                        start.scheme != "http"
                        or start.hostname != "127.0.0.1"
                        or not start.port
                        or not token
                        or not stamp
                    ):
                        raise ValueError("Invalid dashboard startup binding")
                    reference = "setup.dashboard.session"
                    asyncio.run(self._store.set_secret(reference, token))
                    record = ModelSetupDashboardProcess(
                        pid=process.pid,
                        process_stamp=stamp,
                        url=f"http://127.0.0.1:{start.port}",
                        credential_ref=reference,
                        tenant_id=str(identity.tenant_uuid),
                    )
                    ready_deadline = time.monotonic() + 15
                    while time.monotonic() < ready_deadline:
                        try:
                            if self._ready(record):
                                self._write(record)
                                return
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    raise RuntimeError(
                        "The dashboard did not pass authenticated readiness"
                    )
                raise RuntimeError("The dashboard startup timed out")
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                raise
            finally:
                if process.stdout is not None:
                    process.stdout.close()
        finally:
            os.close(fd)
