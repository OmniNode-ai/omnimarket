# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One Claude Code call that proves the credential in the host's environment (OMN-21626).

The credential is ``CLAUDE_CODE_OAUTH_TOKEN`` in this process's environment, put there by the caller on
the host whose pool holds it. This handler never reads, stores or returns it: the child inherits it, and
every ``sk-ant-`` string in the answer is redacted before the bounded excerpt leaves.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_claude_credential_probe import (
    ModelClaudeCredentialProbeReceipt,
    ModelClaudeCredentialProbeRequest,
)

_REDACT = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


class ModelClaudeCredentialProbePolicy(BaseModel):
    """Contract values for the probe: the executable, its flags, the deadline ceiling and the excerpt size."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executable: str = Field(min_length=1)
    argv: tuple[str, ...]
    scrubbed_env: tuple[str, ...] = ()
    max_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    terminate_grace_seconds: float = Field(gt=0, le=30)
    output_chars: int = Field(gt=0)


def load_claude_credential_probe_policy() -> ModelClaudeCredentialProbePolicy:
    """The probe policy packaged in this node's contract."""
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_llm_delegation_call_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    return ModelClaudeCredentialProbePolicy.model_validate(
        contract["claude_credential_probe"]
    )


def _terminate(process: subprocess.Popen[str], grace: float) -> str:
    """Reap this probe's process group only."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            return process.communicate(timeout=grace)[0]
        except subprocess.TimeoutExpired:
            continue
    return process.communicate()[0]


class HandlerClaudeCredentialProbe:
    """Run one bounded Claude Code call and return a redacted, typed receipt."""

    def __init__(self, policy: ModelClaudeCredentialProbePolicy | None = None) -> None:
        self.policy = (
            policy if policy is not None else load_claude_credential_probe_policy()
        )

    def handle(
        self, request: ModelClaudeCredentialProbeRequest
    ) -> ModelClaudeCredentialProbeReceipt:
        if request.timeout_seconds > self.policy.max_timeout_seconds:
            raise ValueError("requested timeout exceeds contract policy")
        started = time.monotonic()
        argv = [
            self.policy.executable,
            *self.policy.argv,
            "--model",
            request.model,
        ]
        env = {k: v for k, v in os.environ.items() if k not in self.policy.scrubbed_env}
        try:
            process = subprocess.Popen(
                argv,
                cwd=Path.home(),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError:
            return ModelClaudeCredentialProbeReceipt(
                wall_seconds=time.monotonic() - started,
                model=request.model,
                exit_cause="launch_failed",
            )
        timed_out = False
        try:
            output = process.communicate(
                request.prompt, timeout=request.timeout_seconds
            )[0]
        except subprocess.TimeoutExpired:
            timed_out = True
            output = _terminate(process, self.policy.terminate_grace_seconds)
        except BaseException:
            _terminate(process, self.policy.terminate_grace_seconds)
            raise
        code = process.returncode
        cause = (
            "timeout" if timed_out else "answered" if code == 0 else "process_failed"
        )
        return ModelClaudeCredentialProbeReceipt(
            output=_REDACT.sub("<redacted>", output or "").strip()[
                : self.policy.output_chars
            ],
            wall_seconds=time.monotonic() - started,
            model=request.model,
            exit_cause=cause,
            exit_code=code,
        )


__all__ = [
    "HandlerClaudeCredentialProbe",
    "ModelClaudeCredentialProbePolicy",
    "load_claude_credential_probe_policy",
]
