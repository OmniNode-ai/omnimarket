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
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument
from omnimarket.protocols.protocol_process_environment import (
    ProtocolProcessEnvironment,
)

_CREDENTIAL_SUFFIXES = ("_KEY", "_TOKEN", "_PASSWORD")
# The settings store overlay that holds each Claude tier's credential order and effort (OMN-26219). No mode and no
# effort is chosen in code: a tier with no row is refused by the row's name.
_TIER_SETTINGS_DOCUMENT = "claude_harness_tiers"
# A key or token the child would inherit is dropped from a login launch, so a login never bills a key.
_INHERITED_PREFIX = "ANTHROPIC_"
# A result record whose text says the subscription login is limited or absent: the next declared credential
# is tried, or the call is refused by name.
_LOGIN_UNUSABLE_MARKERS = (
    "not logged in",
    "please run /login",
    "usage limit",
    "limit reached",
    "rate limit",
    "session limit",
    "credit balance",
)


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
    if record.get("is_error") and any(
        marker in answer.lower() for marker in _LOGIN_UNUSABLE_MARKERS
    ):
        return ModelHarnessRunReceipt(
            wall_seconds=wall,
            exit_cause="not_logged_in",
            exit_code=code,
            detail="the login is limited or absent",
        )
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
        settings: ProtocolOverlayDocument | None = None,
    ) -> None:
        self._settings = settings
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

    def _tier_setting(self, backend_id: str) -> tuple[dict[str, Any] | None, str]:
        """The settings store row of one Claude tier, or ``None`` and the name of the row that is absent."""
        name = f"{_TIER_SETTINGS_DOCUMENT}.tiers.{backend_id}"
        document = (
            self._settings.read(_TIER_SETTINGS_DOCUMENT)
            if self._settings is not None
            else None
        )
        tiers = document.get("tiers") if isinstance(document, dict) else None
        row = tiers.get(backend_id) if isinstance(tiers, dict) else None
        if not isinstance(row, dict) or not row.get("credential_order"):
            return None, name
        return row, name

    async def _claude_environment(
        self, harness: EnumDelegationHarness, mode: str = "subscription"
    ) -> tuple[dict[str, str] | None, str]:
        """The child environment, or ``None`` and the named reason the route or credential is unusable.

        ``mode`` is ``api_key`` (the agent entry's declared ``secret_env`` is resolved from the secret store by its
        declared name into the child environment alone) or ``subscription`` (the login: no key is inherited).
        """
        env = _agent_environment(self._environment)
        config = self._agents.get(harness.value, {})
        glm = harness is EnumDelegationHarness.CLAUDE_GLM
        declared = dict(config.get("env", {}))
        secrets = dict(config.get("secret_env", {}))
        if any(key.endswith(_CREDENTIAL_SUFFIXES) for key in declared):
            return (
                None,
                "a credential is declared as a plain value in env, not secret_env",
            )
        if glm and (not secrets or not declared.get("ANTHROPIC_BASE_URL")):
            return None, "the route or its secret reference is not declared"
        for key in [k for k in env if k.startswith(_INHERITED_PREFIX)]:
            if key.endswith(_CREDENTIAL_SUFFIXES):
                env.pop(key)
        if mode == "subscription":
            secrets = {}
        elif not secrets:
            return None, "the agent entry declares no secret_env for an API key"
        for key, ref in secrets.items():
            secret = await resolve_api_key_async(ref, required=True)
            if secret is None:
                return None, f"{key} did not resolve from the secret store entry {ref}"
            env[key] = secret.get_secret_value()
        env.update(declared)
        return env, ""

    async def _run_claude(
        self, backend: ModelDelegationBackendConfig, prompt: str, timeout_seconds: float
    ) -> ModelHarnessRunReceipt:
        started = time.monotonic()
        harness = backend.harness
        assert harness is not None
        glm = harness is EnumDelegationHarness.CLAUDE_GLM
        adapter = HandlerClaudeGlmArguments() if glm else HandlerClaudeCodeArguments()
        if not backend.model_name:
            return ModelHarnessRunReceipt(
                wall_seconds=time.monotonic() - started, exit_cause="launch_failed"
            )
        argv = adapter.argv(None, model=backend.model_name)
        if glm:
            modes = [
                "api_key"
            ]  # the GLM route's one declared credential, resolved from its secret_env
        else:
            row, name = self._tier_setting(backend.backend_id)
            if row is None:
                return ModelHarnessRunReceipt(
                    wall_seconds=time.monotonic() - started,
                    exit_cause="credential_refused",
                    detail=f"the setting {name} is absent: no credential mode is chosen in code",
                )
            modes = [str(item) for item in row["credential_order"]]
            if row.get("effort"):
                argv += ["--effort", str(row["effort"])]
        reasons: list[str] = []
        for mode in modes:
            try:
                env, reason = await self._claude_environment(harness, mode)
            except (OSError, ValueError):
                env, reason = None, f"the {mode} credential could not be resolved"
            if env is None:
                # The declared route or credential is unusable: refuse by name.
                reasons.append(reason)
                continue
            receipt = await self._launch_claude(env, argv, prompt, timeout_seconds)
            if receipt.exit_cause != "not_logged_in":
                return receipt
            reasons.append(f"{mode}: {receipt.detail or 'login unusable'}")
        return ModelHarnessRunReceipt(
            wall_seconds=time.monotonic() - started,
            exit_cause="not_logged_in",
            detail="no usable Claude credential: " + "; ".join(reasons),
        )

    async def _launch_claude(
        self,
        env: dict[str, str],
        argv: list[str],
        prompt: str,
        timeout_seconds: float,
    ) -> ModelHarnessRunReceipt:
        started = time.monotonic()
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
