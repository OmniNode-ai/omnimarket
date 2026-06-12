# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-dispatch-path regression: _extract_row must unwrap ModelEventEnvelope.

OMN-13099 — probe4 degenerate while generation succeeds.

Root cause (STOP_TRACE.md, 2026-06-12):
  ``EventBusKafka.publish()`` wraps the raw ``ModelGenerationBenchmark`` bytes in a
  ``ModelEventEnvelope`` before writing to Kafka. The wire format is:

    {
      "payload": { "correlation_id": "47ac64f1-...", "attempt_count": 1, ... },
      "envelope_id": "aca26c9f-...",
      "correlation_id": "47ac64f1-...",
      ...
    }

  ``HandlerContextRoiRunner._extract_row()`` checked ``"attempt_count" not in
  event_payload`` against the *outer* envelope dict. That check is ``True`` because
  ``attempt_count`` lives under ``event_payload["payload"]``, not at the top level.
  The CID match succeeded (envelope copies ``correlation_id`` to top level), so the
  terminal event WAS delivered — but ``_extract_row`` fail-closed with
  ``failure_stage=GENERATION, attempt_count=0, model_id=""``.

Fix:
  At the start of ``_extract_row``, detect a ``"payload"`` key and use
  ``event_payload["payload"]`` as the telemetry dict (backwards-compatible: if no
  ``"payload"`` key the dict is already raw and is used as-is).

These tests drive the REAL ``HandlerContextRoiRunner`` through the
``_FakePerTopicTwoPhaseConsumer`` infrastructure (same pattern as
``test_failed_terminal_routing.py``), injecting an envelope-wrapped terminal that
exactly mirrors the live wire format observed in probe4.

Red-before / green-after:
  Tests FAIL on any commit where ``_extract_row`` does NOT unwrap the envelope.
  Tests PASS after the unwrap fix is applied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.context_roi import EnumFailureStage
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)

_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)

# ---------------------------------------------------------------------------
# Wire-format terminal payloads as observed by probe4 (STOP_TRACE.md)
# ---------------------------------------------------------------------------

# Raw benchmark fields — what node_generation_consumer emits to ModelGenerationBenchmark.
_RAW_BENCHMARK: dict[str, Any] = {
    "correlation_id": "47ac64f1-8848-431e-8edd-94a79cff1b2f",
    "attempt_count": 1,
    "contract_passed": True,
    "first_pass_success": True,
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "cost_inference_usd": 0.001,
    "model_id": "Qwen3.6-35B-A3B",
    "provider": "local",
    "endpoint_class": "local-coder",
}

# Envelope-wrapped format that EventBusKafka.publish() produces on the wire.
# The outer envelope copies correlation_id to the top level (CID match succeeds),
# but all telemetry fields (attempt_count, provider, model_id, …) are nested
# under "payload".
_ENVELOPE_WRAPPED_BENCHMARK: dict[str, Any] = {
    "payload": _RAW_BENCHMARK,
    "envelope_id": str(uuid4()),
    "envelope_timestamp": "2026-06-12T14:17:26.587511Z",
    "correlation_id": _RAW_BENCHMARK["correlation_id"],  # copied to top-level
    "event_type": "omnimarket.node-generation-completed",
    "source_tool": "node_generation_consumer",
    "target_tool": "",
    "schema_version": "1.0.0",
}

# Same envelope shape for a failed terminal (contract_passed=False).
_RAW_FAILED_BENCHMARK: dict[str, Any] = {
    **_RAW_BENCHMARK,
    "correlation_id": "d173fc34-09a1-4884-9efb-1d5ef764e3d1",
    "attempt_count": 2,
    "contract_passed": False,
    "first_pass_success": False,
}

_ENVELOPE_WRAPPED_FAILED: dict[str, Any] = {
    "payload": _RAW_FAILED_BENCHMARK,
    "envelope_id": str(uuid4()),
    "envelope_timestamp": "2026-06-12T14:17:27.000000Z",
    "correlation_id": _RAW_FAILED_BENCHMARK["correlation_id"],
    "event_type": "omnimarket.node-generation-failed",
    "source_tool": "node_generation_consumer",
    "target_tool": "",
    "schema_version": "1.0.0",
}


# ---------------------------------------------------------------------------
# Two-phase fake consumer delivering per-topic payloads
# ---------------------------------------------------------------------------


class _FakeTerminalSession:
    """Per-topic two-phase session that delivers a configured payload on wait()."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload
        self.closed = False

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        if self._payload is None:
            return None
        return {**self._payload, "correlation_id": correlation_id}

    def close(self) -> None:
        self.closed = True


class _FakePerTopicConsumer:
    """Two-phase consumer delivering a configured payload per topic substr."""

    def __init__(
        self, payload_by_topic_substr: dict[str, dict[str, Any] | None]
    ) -> None:
        self._by_topic = payload_by_topic_substr
        self.sessions: list[_FakeTerminalSession] = []

    def _payload_for(self, topic: str) -> dict[str, Any] | None:
        for substr, payload in self._by_topic.items():
            if substr in topic:
                return payload
        raise AssertionError(f"unexpected terminal topic: {topic!r}")

    def open(self, terminal_topic: str) -> _FakeTerminalSession:
        session = _FakeTerminalSession(self._payload_for(terminal_topic))
        self.sessions.append(session)
        return session

    def __call__(
        self, terminal_topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        raise AssertionError("must use two-phase open().wait() path")


def _make_handler(
    consumer: object,
) -> HandlerContextRoiRunner:
    def _noop_publisher(topic: str, payload: bytes) -> None:
        pass

    return HandlerContextRoiRunner(
        event_publisher=_noop_publisher,
        event_consumer=consumer,  # type: ignore[arg-type]
        runner_contract_path=_CONTRACT_PATH,
    )


def _make_request(timeout_seconds: float = 5.0) -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-omn13099",
        tasks=(
            ModelContextRoiTask(
                task_id="invoice_reconcile",
                task_description="Generate a compute node that validates invoices.",
            ),
        ),
        arms=(ModelContextRoiArmSpec(label="off", factor_subset=()),),
        trials_per_cell=1,
        max_attempts=2,
        arm_order_seed=42,
        generation_timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# RED test 1: envelope-wrapped completed terminal must not produce degenerate row
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_envelope_wrapped_completed_terminal_produces_non_degenerate_row() -> None:
    """RED before OMN-13099 fix; GREEN after.

    The consumer delivers an envelope-wrapped terminal (the exact wire format
    EventBusKafka.publish() produces). Before the fix, _extract_row checks
    'attempt_count' not in event_payload against the outer envelope dict — True,
    because attempt_count lives under event_payload["payload"] — and fail-closes
    with failure_stage=GENERATION, attempt_count=0.

    After the fix: _extract_row detects the "payload" key and uses
    event_payload["payload"] as the telemetry dict, finding attempt_count=1
    and producing failure_stage=NONE.
    """
    consumer = _FakePerTopicConsumer(
        {
            "generation-completed": _ENVELOPE_WRAPPED_BENCHMARK,
            "generation-failed": None,
        }
    )
    handler = _make_handler(consumer)
    result = handler.handle(_make_request())

    row = result.rows[0]

    assert row.failure_stage != EnumFailureStage.GENERATION, (
        f"failure_stage=GENERATION: _extract_row did not unwrap the envelope — "
        f"probe4 degenerate-row defect (OMN-13099). "
        f"attempt_count={row.attempt_count}, model_id={row.model_id!r}. "
        "Fix: detect 'payload' key in event_payload and use event_payload['payload'] "
        "as the telemetry dict."
    )
    assert row.failure_stage == EnumFailureStage.NONE, (
        f"expected failure_stage=NONE (contract_passed=True), got {row.failure_stage!r}"
    )
    assert row.attempt_count == 1, (
        f"attempt_count={row.attempt_count}: expected 1 (from envelope payload); "
        "envelope not unwrapped"
    )
    assert row.model_id == "Qwen3.6-35B-A3B", (
        f"model_id={row.model_id!r}: expected 'Qwen3.6-35B-A3B' (from envelope payload)"
    )
    assert row.provider == "local"
    assert row.final_success is True


# ---------------------------------------------------------------------------
# RED test 2: envelope-wrapped FAILED terminal must produce validation failure, not generation failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_envelope_wrapped_failed_terminal_produces_validation_failure() -> None:
    """Envelope-wrapped failed terminal (contract_passed=False) must produce
    failure_stage=VALIDATION with real attempt_count, not the degenerate
    failure_stage=GENERATION / attempt_count=0 signature.

    This exercises the interaction between the OMN-13038 fix (race both terminal
    topics) and the OMN-13099 fix (unwrap envelope before extracting fields).
    Both layers must be present for the row to be non-degenerate.
    """
    consumer = _FakePerTopicConsumer(
        {
            "generation-completed": None,
            "generation-failed": _ENVELOPE_WRAPPED_FAILED,
        }
    )
    handler = _make_handler(consumer)
    result = handler.handle(_make_request())

    row = result.rows[0]

    assert row.failure_stage != EnumFailureStage.GENERATION, (
        f"failure_stage=GENERATION: _extract_row did not unwrap the failed envelope — "
        f"attempt_count={row.attempt_count}, model_id={row.model_id!r}. "
        "Both OMN-13038 (race failed.v1) and OMN-13099 (envelope unwrap) are required."
    )
    assert row.failure_stage == EnumFailureStage.VALIDATION, (
        f"expected failure_stage=VALIDATION (contract_passed=False), got {row.failure_stage!r}"
    )
    assert row.attempt_count == 2, (
        f"expected attempt_count=2 from envelope payload, got {row.attempt_count}"
    )
    assert row.final_success is False
    assert row.model_id == "Qwen3.6-35B-A3B"
    assert row.provider == "local"


# ---------------------------------------------------------------------------
# Test 3: raw (unwrapped) dict must still work (backwards compat)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_raw_dict_terminal_still_works_after_unwrap_fix() -> None:
    """Regression: a raw (non-envelope) terminal dict must keep working after the
    unwrap fix. If the fix unconditionally accesses event_payload['payload'] it
    would break existing tests that pass raw dicts.
    """
    raw_benchmark: dict[str, Any] = {
        "attempt_count": 1,
        "contract_passed": True,
        "first_pass_success": True,
        "prompt_tokens": 50,
        "completion_tokens": 30,
        "cost_inference_usd": 0.0005,
        "model_id": "Qwen3.6-35B-A3B",
        "provider": "local",
        "endpoint_class": "local-coder",
    }

    consumer = _FakePerTopicConsumer(
        {
            "generation-completed": raw_benchmark,
            "generation-failed": None,
        }
    )
    handler = _make_handler(consumer)
    result = handler.handle(_make_request())

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.NONE, (
        f"raw-dict path broken after unwrap fix: failure_stage={row.failure_stage!r}"
    )
    assert row.attempt_count == 1
    assert row.model_id == "Qwen3.6-35B-A3B"


# ---------------------------------------------------------------------------
# Test 4: handle_async (runtime entry point) also unwraps envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_async_unwraps_envelope() -> None:
    """The runtime invokes handle_async. Verify the envelope-unwrap fix applies
    to the async entry point as well."""
    consumer = _FakePerTopicConsumer(
        {
            "generation-completed": _ENVELOPE_WRAPPED_BENCHMARK,
            "generation-failed": None,
        }
    )
    handler = _make_handler(consumer)
    result = await handler.handle_async(_make_request())

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.NONE, (
        f"handle_async did not unwrap the envelope: failure_stage={row.failure_stage!r}"
    )
    assert row.attempt_count == 1
    assert row.model_id == "Qwen3.6-35B-A3B"
