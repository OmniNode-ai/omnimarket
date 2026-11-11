# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose the existing native gateway and Claude settings owners for setup."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from omnibase_core.cli.cli_user_config import user_config_path

from omnimarket.handlers.claude_gateway_settings import (
    HandlerClaudeGatewaySettings,
    ModelClaudeGatewaySettingsRequest,
    probe_gateway,
)
from omnimarket.handlers.handler_endpoint_connections import (
    HandlerModelConnections,
    ModelEndpointConnection,
    ModelEndpointConnectionRequest,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import read_local_tenant_identity
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
    _stamp,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_gateway import (
    ModelSetupGatewayProcess,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


class HandlerSetupGateway:
    """One owned process, canonical credential references, and one native proof."""

    name = "claude-gateway"

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        store: LocalByokCredentialStore | None = None,
    ) -> None:
        self._home = onex_home or user_config_path().parent
        self._state = self._home / "gateway-process.json"
        self._store = store or LocalByokCredentialStore()

    def _settings(
        self, plan: ModelSetupPlan | None = None
    ) -> HandlerClaudeGatewaySettings:
        return HandlerClaudeGatewaySettings(
            onex_home=self._home,
            working_directory=plan.workspace if plan else None,
            probe=partial(probe_gateway, read_secret=self._store.get_secret),
        )

    def _read(self) -> ModelSetupGatewayProcess | None:
        try:
            fd = os.open(self._state, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError("Invalid gateway process metadata")
            return ModelSetupGatewayProcess.model_validate_json(source.read(65537))

    def _write(self, record: ModelSetupGatewayProcess) -> None:
        fd, name = tempfile.mkstemp(prefix=".gateway-", dir=self._home)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as target:
                target.write(record.model_dump_json() + "\n")
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(self._state)
        finally:
            temporary.unlink(missing_ok=True)

    def _connection(self, plan: ModelSetupPlan) -> ModelEndpointConnection:
        if plan.gateway is None:
            raise ValueError(
                "Select a named native Messages connection and model for Claude"
            )
        result = HandlerModelConnections(db_path=self._store.db_path).handle(
            ModelEndpointConnectionRequest(
                operation="list", connection_id=plan.gateway.connection_id
            )
        )
        if result.status != "ready" or len(result.connections) != 1:
            raise ValueError("Configure the selected named model connection first")
        connection = result.connections[0]
        if (
            not connection.native_messages
            or plan.gateway.model not in connection.models
        ):
            raise ValueError(
                "The selected connection must explicitly declare native Messages and contain the chosen model"
            )
        return connection

    @staticmethod
    def _digest(connection: ModelEndpointConnection) -> str:
        return hashlib.sha256(connection.model_dump_json().encode()).hexdigest()

    def _request(
        self, record: ModelSetupGatewayProcess, action: str = "enable"
    ) -> ModelClaudeGatewaySettingsRequest:
        helper = shlex.join(
            [
                str(Path(sys.executable).absolute()),
                "-m",
                "omnimarket.nodes.node_claude_code_binding_install_effect.cli_gateway",
                "credential",
                "--ref",
                record.credential_ref,
            ]
        )
        return ModelClaudeGatewaySettingsRequest.model_validate(
            {
                "action": action,
                "gateway_url": record.url,
                "model": record.selection.model,
                "credential_ref": record.credential_ref,
                "credential_helper": helper,
                "environment": dict(os.environ),
            }
        )

    def _ready(
        self,
        record: ModelSetupGatewayProcess,
        plan: ModelSetupPlan,
        connection: ModelEndpointConnection,
    ) -> bool:
        identity = read_local_tenant_identity(db_path=self._store.db_path)
        return bool(
            identity
            and str(identity.tenant_uuid) == record.tenant_id
            and record.selection == plan.gateway
            and record.connection_digest == self._digest(connection)
            and _stamp(record.pid) == record.process_stamp
            and probe_gateway(self._request(record), read_secret=self._store.get_secret)
        )

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            connection = self._connection(plan)
            record = self._read()
            if (
                record
                and record.proved
                and record.proof_run_id is not None
                and self._ready(record, plan, connection)
            ):
                binding = self._settings(plan).handle(self._request(record, "status"))
                if (
                    binding.status == "success"
                    and binding.gateway_url == record.url
                    and binding.model == record.selection.model
                ):
                    return ModelSetupObservation(
                        step=self.name,
                        status="success",
                        evidence={
                            "url": record.url,
                            "backend_id": record.backend_id,
                            "model": record.selection.model,
                            "pid": str(record.pid),
                            "native_message_proof": "passed",
                            "proof_run_id": str(record.proof_run_id),
                            "correlation_id": str(record.proof_run_id),
                            "tool_stream_compatibility": "requires separate client proof",
                        },
                    )
            return ModelSetupObservation(
                step=self.name,
                status="unavailable",
                fix="Run onex setup --action repair with the selected native connection and model.",
            )
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Configure a named connection with native Messages explicitly enabled, then select its model for Claude gateway setup.",
            )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if plan.action != "verify":
            self.apply(plan)
        return self.observe(plan)

    def _stop_process(self, record: ModelSetupGatewayProcess) -> None:
        if _stamp(record.pid) != record.process_stamp:
            return
        os.kill(record.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while (
            time.monotonic() < deadline and _stamp(record.pid) == record.process_stamp
        ):
            time.sleep(0.05)
        if _stamp(record.pid) == record.process_stamp:
            raise RuntimeError("Owned gateway has not stopped; no replacement started")

    def stop(self) -> None:
        if not self._home.is_dir():
            return
        fd = os.open(
            self._home / "gateway-process.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._stop_locked()
        finally:
            os.close(fd)

    def _stop_locked(self) -> None:
        record = self._read()
        if record is None:
            return
        result = self._settings().handle(self._request(record, "disable"))
        if result.status not in ("success", "action_required"):
            raise RuntimeError("Claude gateway settings could not be disabled")
        self._stop_process(record)

    def apply(self, plan: ModelSetupPlan) -> None:
        connection = self._connection(plan)
        assert plan.gateway is not None
        identity = read_local_tenant_identity(db_path=self._store.db_path)
        if identity is None:
            raise ValueError("Run onex local init before starting the gateway")
        self._home.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            self._home / "gateway-process.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.observe(plan).status == "success":
                return
            previous = self._read()
            if previous:
                self._stop_locked()
            port = plan.gateway.port
            if not port:
                with socket.socket() as candidate:
                    candidate.bind(("127.0.0.1", 0))
                    port = candidate.getsockname()[1]
            reference = "setup.gateway." + uuid4().hex
            asyncio.run(self._store.set_secret(reference, secrets.token_urlsafe(32)))
            backend = connection.backend_id(plan.gateway.model, native=True)
            environment = {
                key: value for key, value in os.environ.items() if key != "PYTHONPATH"
            }
            process = subprocess.Popen(
                [
                    str(Path(sys.executable).parent / "onex"),
                    "claude",
                    "gateway",
                    "--backend-id",
                    backend,
                    "--model",
                    plan.gateway.model,
                    "--credential-ref",
                    reference,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=Path.home(),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                stamp = _stamp(process.pid)
                if not stamp:
                    raise RuntimeError("Gateway process did not start")
                record = ModelSetupGatewayProcess(
                    pid=process.pid,
                    process_stamp=stamp,
                    url=f"http://127.0.0.1:{port}",
                    credential_ref=reference,
                    tenant_id=str(identity.tenant_uuid),
                    selection=plan.gateway,
                    backend_id=backend,
                    connection_digest=self._digest(connection),
                )
                self._write(record)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Gateway process exited before readiness")
                    if self._ready(record, plan, connection):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Gateway route did not become ready")
                token = asyncio.run(self._store.get_secret(reference))
                with httpx.Client(trust_env=False, timeout=60) as client:
                    response = client.post(
                        record.url + "/v1/messages",
                        headers={"authorization": "Bearer " + (token or "")},
                        json={
                            "model": plan.gateway.model,
                            "max_tokens": 8,
                            "messages": [
                                {"role": "user", "content": "Reply with hello."}
                            ],
                        },
                    )
                    response.raise_for_status()
                    body = response.json()
                if (
                    body.get("model") != plan.gateway.model
                    or not body.get("content")
                    or not isinstance(body.get("usage"), dict)
                ):
                    raise ValueError(
                        "Native model proof did not return its model and usage"
                    )
                # This header is minted by our owned gateway only after its
                # canonical receipt/projection write, never copied upstream.
                proof_run_id = UUID(response.headers.get("x-onex-run-id", ""))
                record = record.model_copy(
                    update={"proved": True, "proof_run_id": proof_run_id}
                )
                self._write(record)
                bound = self._settings(plan).handle(self._request(record))
                if bound.status != "success":
                    raise ValueError(
                        "Claude settings conflict with the reviewed gateway"
                    )
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                raise
        finally:
            os.close(fd)


def stop_gateway(onex_home: Path) -> None:
    HandlerSetupGateway(onex_home=onex_home).stop()


def start_gateway(onex_home: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
        HandlerSetupWorkflow,
    )

    plan = HandlerSetupWorkflow.saved_plan(onex_home)
    if plan is None or plan.gateway is None:
        raise ValueError("Run onex setup to select a native gateway connection first")
    HandlerSetupGateway(onex_home=onex_home).apply(plan)
