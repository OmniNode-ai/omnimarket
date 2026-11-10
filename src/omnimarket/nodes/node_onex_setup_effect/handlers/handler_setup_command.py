# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute bounded argv without a shell or inherited workspace binding."""

import os
import subprocess

from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)


class HandlerSetupCommand:
    """One command effect; known credentials never enter argv, env or results."""

    def __init__(self, secrets: tuple[SecretStr, ...] = ()) -> None:
        self._secrets = tuple(
            sorted(
                {
                    item.get_secret_value()
                    for item in secrets
                    if item.get_secret_value()
                },
                key=len,
                reverse=True,
            )
        )

    def _redact(self, value: str | bytes | None) -> str:
        text = (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else value or ""
        )
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def handle(self, request: ModelSetupCommandRequest) -> ModelSetupCommandResult:
        """Return redacted output even when a process times out or cannot start."""
        if request.stdin is not None:
            scoped = HandlerSetupCommand(
                secrets=(*(SecretStr(value) for value in self._secrets), request.stdin)
            )
            return scoped._run(request)
        return self._run(request)

    def _run(self, request: ModelSetupCommandRequest) -> ModelSetupCommandResult:
        for value in (*request.argv, *request.environment.values()):
            if any(secret in value for secret in self._secrets):
                raise ValueError("A secret cannot be passed in argv or environment")
        environment = os.environ.copy()
        for name in (
            "PYTHONPATH",
            "OMNI_HOME",
            "OMNIBASE_PATH",
            "ONEX_CONTRACTS_DIR",
            "ONEX_CONFIG_DIR",
            "ONEX_TENANT_ID",
            "ONEX_TENANT_SLUG",
        ):
            environment.pop(name, None)
        for name, value in tuple(environment.items()):
            if any(secret in value for secret in self._secrets):
                environment.pop(name)
        environment.update(request.environment)
        try:
            completed = subprocess.run(
                request.argv,
                cwd=request.cwd,
                input=request.stdin.get_secret_value()
                if request.stdin is not None
                else None,
                text=True,
                capture_output=True,
                check=False,
                timeout=request.timeout_seconds,
                env=environment,
            )
            return ModelSetupCommandResult(
                argv=request.argv,
                returncode=completed.returncode,
                stdout=self._redact(completed.stdout),
                stderr=self._redact(completed.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            return ModelSetupCommandResult(
                argv=request.argv,
                returncode=124,
                timed_out=True,
                stdout=self._redact(exc.stdout),
                stderr="Command timed out.\n" + self._redact(exc.stderr),
            )
        except OSError:
            return ModelSetupCommandResult(
                argv=request.argv,
                returncode=127,
                stdout="",
                stderr="Command could not start.",
            )
