# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one prompt through a declared harness: Claude Code (also on GLM) or Codex (OMN-26219).

The argument builders of ``handler_delegation_target`` and ``HandlerCodexHarnessAttempt``
are reused as they are. GLM runs only through Claude Code (OMN-20173), never a direct
endpoint call. The child runs in a clean temporary directory with no repository; the
credential references and the provider route come from the deployment overlay's
``delegation_adapters`` agents, and no credential value reaches a receipt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationHarness,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_codex_harness_attempt import (
    HandlerCodexHarnessAttempt,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerClaudeCodeArguments,
    HandlerClaudeGlmArguments,
    _agent_environment,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt import (
    ModelCodexHarnessRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    HarnessExitCause,
    ModelHarnessRunReceipt,
)
from omnimarket.protocols.protocol_process_environment import (
    ProtocolProcessEnvironment,
)

_CREDENTIAL_SUFFIXES = ("_KEY", "_TOKEN", "_PASSWORD")
# The Claude subscription login is the only credential the ``claude`` harness runs on: an API key or an
# auth token in the child's environment would bill a key instead (OMN-26043), so each is refused by name.
_SUBSCRIPTION_REFUSED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _answering_model(record: dict[str, Any]) -> str | None:
    """The model the result record reports it ran: its one ``modelUsage`` key, else the heaviest by output."""
    usage = record.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    if len(usage) == 1:
        return str(next(iter(usage)))

    def output(name: object) -> int:
        row = usage[name]
        value = row.get("outputTokens") if isinstance(row, dict) else None
        return value if type(value) is int else 0

    return str(max(usage, key=output))


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _result_record(stdout: str) -> dict[str, Any] | None:
    """The ``result`` record of a ``claude -p --output-format json`` run, or ``None``."""
    try:
        loaded = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    records = loaded if isinstance(loaded, list) else [loaded]
    found = [r for r in records if isinstance(r, dict) and r.get("type") == "result"]
    return found[-1] if found else None


def _claude_receipt(
    stdout: str, *, code: int | None, started: float
) -> ModelHarnessRunReceipt:
    wall = time.monotonic() - started
    record = _result_record(stdout)
    if record is None:
        failed: HarnessExitCause = "process_failed" if code else "invalid_output"
        return ModelHarnessRunReceipt(
            wall_seconds=wall, exit_cause=failed, exit_code=code
        )
    raw_usage = record.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    cost: Decimal | None = None
    reported = record.get("total_cost_usd")
    if isinstance(reported, (int, float, str)) and not isinstance(reported, bool):
        with contextlib.suppress(InvalidOperation, ValueError):
            cost = Decimal(str(reported))
            if cost < 0 or not cost.is_finite():
                cost = None
    answer = str(record.get("result", ""))
    cause: HarnessExitCause = "completed"
    if code:
        cause = "process_failed"
    elif record.get("is_error"):
        cause = "turn_failed"
    elif not answer.strip():
        cause = "invalid_output"
    return ModelHarnessRunReceipt(
        answer=answer if cause == "completed" else "",
        tokens_in=_count(usage.get("input_tokens")),
        tokens_out=_count(usage.get("output_tokens")),
        cost_usd=cost,
        wall_seconds=wall,
        exit_cause=cause,
        exit_code=code,
        model=_answering_model(record),
    )


class HandlerHarnessProcessRunner:
    """The process port of the harness call, for the harnesses a backend can declare."""

    def __init__(
        self,
        *,
        environment: ProtocolProcessEnvironment,
        agents: Mapping[str, Mapping[str, Any]],
        codex: HandlerCodexHarnessAttempt | None = None,
    ) -> None:
        self._environment = environment
        self._agents = agents
        self._codex = codex

    async def run(
        self,
        backend: ModelDelegationBackendConfig,
        prompt: str,
        timeout_seconds: float,
    ) -> ModelHarnessRunReceipt:
        harness = backend.harness
        started = time.monotonic()
        if harness is EnumDelegationHarness.CODEX:
            return await self._run_codex(backend, prompt, timeout_seconds)
        if harness not in (
            EnumDelegationHarness.CLAUDE_GLM,
            EnumDelegationHarness.CLAUDE,
        ):
            return ModelHarnessRunReceipt(
                wall_seconds=time.monotonic() - started, exit_cause="launch_failed"
            )
        return await self._run_claude(backend, prompt, timeout_seconds)

    async def _run_codex(
        self, backend: ModelDelegationBackendConfig, prompt: str, timeout_seconds: float
    ) -> ModelHarnessRunReceipt:
        started = time.monotonic()
        if self._codex is None:
            return ModelHarnessRunReceipt(wall_seconds=0.0, exit_cause="launch_failed")
        limit = self._codex.policy.max_timeout_seconds
        attempt = await asyncio.to_thread(
            self._codex.handle,
            ModelCodexHarnessRequest(
                prompt=prompt,
                model=backend.model_name,
                timeout_seconds=min(timeout_seconds, limit),
            ),
        )
        return ModelHarnessRunReceipt(
            answer=attempt.answer,
            tokens_in=attempt.tokens_in,
            tokens_out=attempt.tokens_out,
            wall_seconds=attempt.wall_seconds or (time.monotonic() - started),
            exit_cause=attempt.exit_cause,
            exit_code=attempt.exit_code,
        )

    def _subscription_violation(self, harness: EnumDelegationHarness) -> str | None:
        """The name of a variable that would put an API key behind the subscription harness, or ``None``."""
        config = self._agents.get(harness.value, {})
        declared = {
            *self._environment.environment(),
            *config.get("env", {}),
            *config.get("secret_env", {}),
        }
        return next((name for name in _SUBSCRIPTION_REFUSED if name in declared), None)

    async def _claude_environment(
        self, harness: EnumDelegationHarness
    ) -> dict[str, str] | None:
        """The child environment, or ``None`` when the declared route or credential is unusable."""
        env = _agent_environment(self._environment)
        config = self._agents.get(harness.value, {})
        glm = harness is EnumDelegationHarness.CLAUDE_GLM
        declared = dict(config.get("env", {}))
        secrets = dict(config.get("secret_env", {}))
        if any(key.endswith(_CREDENTIAL_SUFFIXES) for key in declared):
            return None
        if glm:
            if not secrets or not declared.get("ANTHROPIC_BASE_URL"):
                return None
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        for key, ref in secrets.items():
            secret = await resolve_api_key_async(ref, required=True)
            if secret is None:
                return None
            env[key] = secret.get_secret_value()
        env.update(declared)
        return env

    async def _run_claude(
        self, backend: ModelDelegationBackendConfig, prompt: str, timeout_seconds: float
    ) -> ModelHarnessRunReceipt:
        started = time.monotonic()
        harness = backend.harness
        assert harness is not None
        glm = harness is EnumDelegationHarness.CLAUDE_GLM
        adapter = HandlerClaudeGlmArguments() if glm else HandlerClaudeCodeArguments()
        if not glm:
            present = self._subscription_violation(harness)
            if present is not None:
                return ModelHarnessRunReceipt(
                    wall_seconds=time.monotonic() - started,
                    exit_cause="credential_refused",
                    detail=(
                        f"{present} is set: the claude harness runs on the "
                        "subscription login and never an API key"
                    ),
                )
        try:
            env = await self._claude_environment(harness)
        except (OSError, ValueError):
            env = None
        if env is None or not backend.model_name:
            return ModelHarnessRunReceipt(
                wall_seconds=time.monotonic() - started, exit_cause="launch_failed"
            )
        argv = adapter.argv(None, model=backend.model_name)
        if backend.effort is not None:
            argv += ["--effort", backend.effort]
        with tempfile.TemporaryDirectory(prefix="harness-call-") as cwd:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                return ModelHarnessRunReceipt(
                    wall_seconds=time.monotonic() - started, exit_cause="launch_failed"
                )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(prompt.encode()), timeout=timeout_seconds
                )
            except (TimeoutError, asyncio.CancelledError):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return ModelHarnessRunReceipt(
                    wall_seconds=time.monotonic() - started, exit_cause="timeout"
                )
        return _claude_receipt(
            stdout.decode(errors="replace"), code=process.returncode, started=started
        )


__all__ = ["HandlerHarnessProcessRunner"]
