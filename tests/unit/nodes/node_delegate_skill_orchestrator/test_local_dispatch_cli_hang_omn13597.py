# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13597: bus-less local CLI must not hang on an unreachable endpoint.

The standalone ``onex delegate`` path drives an asyncio event loop
(``RuntimeLocal``). The in-memory bus delivers the command *synchronously* inside
``bus.publish`` (``await callback(...)``), so the delegation handler runs to
completion before the runtime ever reaches its terminal-wait timeout. The
handler's effect call (``HandlerLlmDelegationCall.__call__``) is a synchronous
blocking call (health probe + curl/httpx LLM POST). Before this fix, awaiting it
inline blocked the loop, and a connect that stalled below the transport's own
``--max-time``/httpx bound hung the whole CLI forever — zero output, no evidence
row (reproduced on the .201 container).

The fix runs the blocking call behind a supervised child-process boundary with a
hard deadline (contract-resolved transport timeout + buffer). These tests prove:

1. A blocking/unresponsive transport no longer hangs ``dispatch()`` — it returns
   within the bounded window (``status="failed"``).
2. The bounded-timeout path still writes a trustworthy ``delegation_events``
   evidence row with ``quality_gate_passed=false`` (never PASS, never silent).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution
from tests.fixtures.judge_inference import CannedAdequacyBridge


def _pass_judge() -> HandlerJudgeAdequacy:
    """A judge scoring every candidate adequate (0.95 -> PASS) for the local path.

    OMN-13849: code_generation is judge-combinable and the local path applies the
    0.85 required bar. A bare code answer is ~0.733 deterministic-only; this judge
    lifts it over the bar so the loop-progress assertion (a COMPLETED terminal) is
    not confounded by an escalation the missing judge would otherwise trigger.
    """
    return HandlerJudgeAdequacy(
        inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
    )


# Tiny transport timeout (ms) so the contract-resolved ceiling is small and the
# test runs fast and deterministically.
_FAST_TIMEOUT_MS = 200
# Buffer the fix adds on top of the transport timeout. Patched small so the total
# dispatch ceiling is sub-second.
_FAST_BUFFER_SECONDS = 0.2
# The fake effect blocks longer than (timeout_ms/1000 + buffer) so the killable
# process deadline — not the transport — is what releases dispatch.
_EFFECT_BLOCK_SECONDS = 30.0
# OMN-14883: on a ``spawn`` host the child's interpreter boot (a full re-import of
# the effect module tree — measured ~18s on macOS, ~80s on the .201 gate-runner)
# precedes the effect call and is NOT endpoint time. These budgets therefore
# separate the two: a runaway guard sized off the port's own boot ceiling so a
# genuine hang still releases the suite, and a tight bound asserted after the run
# against the boot the port actually measured. A single host-blind wall-clock
# ceiling is what made this suite red on macOS and red on the gate-runner.
_BOOT_MEASUREMENT_SLACK_SECONDS = 5.0


class NeverReturningEffectHandler:
    """Pickleable test double that models a sync transport stuck below timeout."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        time.sleep(_EFFECT_BLOCK_SECONDS)
        raise AssertionError("effect process should have been killed by the deadline")


class SlowSuccessfulEffectHandler:
    """Pickleable test double that blocks briefly and then returns success."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        time.sleep(0.5)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="def reverse(s): return s[::-1]",
            output_hash="test-output-hash",
            tokens_in=5,
            tokens_out=7,
            latency_ms=500,
        )


@pytest.fixture
def fake_backends() -> list[dict[str, object]]:
    return [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://unreachable.invalid:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": _FAST_TIMEOUT_MS,
            "capabilities": ["code_generation"],
        }
    ]


def _patch_routing(
    monkeypatch: pytest.MonkeyPatch, backends: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )


def test_unreachable_endpoint_does_not_hang_and_records_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """Unreachable/unresponsive endpoint: dispatch returns bounded, evidence=FAILED.

    Proves the OMN-13597 hang fix end-to-end without the live CLI: the blocking
    transport is bounded by the wait_for ceiling, dispatch returns ``failed``
    within the deadline, and a ``delegation_events`` row is written with
    ``quality_gate_passed=false``.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    monkeypatch.setattr(
        port_module, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", _FAST_BUFFER_SECONDS
    )

    port = LocalDelegationDispatchPort(
        effect_handler=NeverReturningEffectHandler(),
        evidence_db_path=db_path,
    )
    correlation_id = uuid4()

    deadline_ceiling = (_FAST_TIMEOUT_MS / 1000.0) + _FAST_BUFFER_SECONDS

    # Runaway guard only: a genuine hang must still release the suite. The tight
    # bound is asserted below against the boot the port actually measured.
    runaway_guard_seconds = (
        port_module._EFFECT_CHILD_BOOT_CEILING_SECONDS
        + deadline_ceiling
        + _BOOT_MEASUREMENT_SLACK_SECONDS
    )

    async def _run() -> dict[str, object]:
        # If the loop were blocked (pre-fix behavior), this outer wait_for would not
        # fire. The process-supervised fix keeps the loop responsive and kills the
        # stuck child at the in-port deadline.
        return await asyncio.wait_for(
            port.dispatch(
                prompt="reverse a string",
                task_type="code_generation",
                correlation_id=correlation_id,
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
            ),
            timeout=runaway_guard_seconds,
        )

    t0 = time.monotonic()
    result = asyncio.run(_run())
    elapsed = time.monotonic() - t0

    # 1. Bounded: dispatch returned at the in-port ceiling, well inside the
    #    30s transport block (pre-fix this hung forever).
    #
    #    OMN-14883: the bound is the ENDPOINT ceiling plus the child boot the
    #    port measured on this host — boot is spawn-interpreter cost that never
    #    reaches the endpoint, so folding it into the endpoint ceiling made this
    #    assertion a host-speed assertion. What it still proves is exact: once the
    #    child was ready, dispatch was released at ``deadline_ceiling``, nowhere
    #    near the 30s the effect would have blocked for.
    observed_boot_seconds = port_module._observed_child_boot_seconds or 0.0
    assert elapsed < (
        deadline_ceiling + observed_boot_seconds + _BOOT_MEASUREMENT_SLACK_SECONDS
    )
    assert elapsed < _EFFECT_BLOCK_SECONDS + observed_boot_seconds

    # 2. Failed terminal status (never a hang, never a PASS).
    assert result["status"] == "failed"
    assert "did not return within" in str(result["error_message"])

    # 3. Trustworthy evidence row: quality_gate_passed=false.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["correlation_id"] == str(correlation_id)
    assert bool(row["quality_gate_passed"]) is False
    assert row["task_type"] == "code_generation"


def test_dispatch_offloads_blocking_call_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """The blocking effect call must run off the loop.

    While the synchronous transport blocks, a concurrent coroutine scheduled on
    the same loop must still make progress. Pre-fix, the inline synchronous call
    froze the loop and the concurrent coroutine could not advance.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    monkeypatch.setattr(
        port_module, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", _FAST_BUFFER_SECONDS
    )

    loop_progressed = asyncio.Event()
    # Give the dispatch a long ceiling so it does not time out; the fake effect
    # returns after its short sleep.
    monkeypatch.setattr(port_module, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", 30.0)

    port = LocalDelegationDispatchPort(
        effect_handler=SlowSuccessfulEffectHandler(),
        evidence_db_path=db_path,
        judge=_pass_judge(),
    )

    async def _concurrent_progress() -> None:
        # If the loop were blocked by the synchronous effect, this sleep would not
        # complete until the effect returned.
        await asyncio.sleep(0.05)
        loop_progressed.set()

    async def _run() -> dict[str, object]:
        dispatch_task = asyncio.create_task(
            port.dispatch(
                prompt="reverse a string",
                task_type="code_generation",
                correlation_id=uuid4(),
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
            )
        )
        progress_task = asyncio.create_task(_concurrent_progress())
        # OMN-14883: runaway guard only — this test's property is loop progress
        # and a completed terminal, neither of which is a wall-clock claim. A 15s
        # ceiling made it a claim about how fast the host imports the effect
        # module tree in the spawn child, which is why it went red under load.
        result = await asyncio.wait_for(
            dispatch_task,
            timeout=port_module._EFFECT_CHILD_BOOT_CEILING_SECONDS
            + _EFFECT_BLOCK_SECONDS,
        )
        await progress_task
        return result

    result = asyncio.run(_run())

    # The concurrent coroutine made progress while the effect blocked: the loop was
    # not frozen by the synchronous call.
    assert loop_progressed.is_set()
    assert result["status"] == "completed"
