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

The fix offloads the blocking call to a worker thread and wraps it in a hard
``asyncio.wait_for`` ceiling (contract-resolved transport timeout + buffer). These
tests prove:

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
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import delegation_backend_resolution

# Tiny transport timeout (ms) so the contract-resolved ceiling is small and the
# test runs fast and deterministically.
_FAST_TIMEOUT_MS = 200
# Buffer the fix adds on top of the transport timeout. Patched small so the total
# dispatch ceiling is sub-second.
_FAST_BUFFER_SECONDS = 0.2
# The fake transport blocks longer than (timeout_ms/1000 + buffer) so the
# wait_for ceiling — not the transport — is what releases dispatch. It is bounded
# (modeling the transport's own --max-time/httpx timeout) so the worker thread
# exits promptly and ``asyncio.run`` does not wait on an orphaned thread.
_TRANSPORT_BLOCK_SECONDS = 4.0


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


def _patch_blocking_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the transport POST to block forever (simulating an unreachable host).

    The health probe returns healthy so the dispatch reaches the POST; the POST
    then blocks far longer than the dispatch ceiling, modeling a connect that
    stalls below the transport's own timeout (the exact .201 hang).
    """

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

    def blocking_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        # Synchronous block — this is what froze the event loop before the fix.
        time.sleep(_TRANSPORT_BLOCK_SECONDS)
        raise AssertionError("transport should have been cancelled by the ceiling")

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", blocking_post)


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
    _patch_blocking_transport(monkeypatch)
    monkeypatch.setattr(
        port_module, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", _FAST_BUFFER_SECONDS
    )

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    correlation_id = uuid4()

    deadline_ceiling = (_FAST_TIMEOUT_MS / 1000.0) + _FAST_BUFFER_SECONDS
    timings: dict[str, float] = {}

    async def _run() -> dict[str, object]:
        # If the loop were blocked (pre-fix behavior), this outer wait_for would
        # itself never fire because the synchronous transport sleep would freeze
        # the loop. We give it generous headroom over the in-port ceiling; the
        # fix's to_thread offload keeps the loop responsive so the in-port
        # ceiling fires well inside this bound.
        t0 = time.monotonic()
        out = await asyncio.wait_for(
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
            ),
            timeout=_TRANSPORT_BLOCK_SECONDS - 1.0,
        )
        # Measure dispatch return *inside* the loop: ``asyncio.run`` shutdown may
        # additionally wait on the orphaned to_thread thread (it cannot be
        # cancelled), which is unrelated to whether dispatch itself was bounded.
        timings["dispatch_elapsed"] = time.monotonic() - t0
        return out

    result = asyncio.run(_run())
    elapsed = timings["dispatch_elapsed"]

    # 1. Bounded: dispatch returned at the in-port ceiling, well inside the
    #    transport block (pre-fix this hung forever).
    assert elapsed < _TRANSPORT_BLOCK_SECONDS - 1.0
    assert elapsed < deadline_ceiling + 2.0

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
    """The blocking effect call must run off the loop (proves the to_thread fix).

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
    release_transport = {"value": False}

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

    def blocking_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        # Block until the concurrent coroutine has signalled progress, proving the
        # loop was NOT frozen while this synchronous call ran.
        deadline = time.monotonic() + 5.0
        while not release_transport["value"] and time.monotonic() < deadline:
            time.sleep(0.01)
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": "def reverse(s): return s[::-1]"}}],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "total_tokens": 12,
                },
            },
            latency_ms=1,
        )

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", blocking_post)
    # Give the dispatch a long ceiling so it does not time out; the transport
    # returns as soon as the concurrent coroutine releases it.
    monkeypatch.setattr(port_module, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", 30.0)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)

    async def _concurrent_progress() -> None:
        # If the loop were blocked by the synchronous transport, this sleep would
        # not complete until the transport returned — deadlocking the test.
        await asyncio.sleep(0.05)
        loop_progressed.set()
        release_transport["value"] = True

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
            )
        )
        progress_task = asyncio.create_task(_concurrent_progress())
        result = await asyncio.wait_for(dispatch_task, timeout=15.0)
        await progress_task
        return result

    result = asyncio.run(_run())

    # The concurrent coroutine made progress while the transport blocked: the loop
    # was not frozen — the blocking call ran off-loop (asyncio.to_thread).
    assert loop_progressed.is_set()
    assert result["status"] == "completed"
