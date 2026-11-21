# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""harness_handler_gate_escalation_cost: gate, climb and one cost record per attempt (OMN-26219)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_answer_gate import (
    HandlerHarnessAnswerGate,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_backend_call import (
    HandlerHarnessBackendCall,
)
from tests.unit.delegation.harness_support_omn26219 import (
    ListCostSink,
    ScriptedRunner,
    call_request,
    load_overlay,
    receipt,
    recorded_row,
)

pytestmark = pytest.mark.unit

GOOD_CODE = (
    "```python\n"
    "def add(a: int, b: int) -> int:\n"
    '    """Return the sum of a and b."""\n'
    "    return a + b\n"
    "```\n"
)


def build(runner: ScriptedRunner, sink: ListCostSink) -> HandlerHarnessBackendCall:
    return HandlerHarnessBackendCall(
        runner=runner,
        gate=HandlerHarnessAnswerGate(),
        cost_sink=sink,
        overlay=load_overlay(),
    )


async def test_harness_handler_gate_escalation_cost_climbs_when_the_gate_rejects() -> (
    None
):
    sink = ListCostSink()
    runner = ScriptedRunner(
        [receipt("Sure, I can help."), receipt(GOOD_CODE, cost=Decimal("0.002"))]
    )
    result = await build(runner, sink).handle(call_request("code_generation"))
    assert runner.ran == ["harness-claude-glm-flash", "harness-claude-glm"]
    assert result.status == "completed"
    assert result.model_name == "glm-5.3"
    assert [a.accepted for a in result.attempts] == [False, True]
    assert result.attempts[0].gate_reasons  # the gate said why it rejected
    assert result.attempts[0].gate_kind is not None


async def test_harness_handler_gate_escalation_cost_one_cost_record_per_attempt() -> (
    None
):
    sink = ListCostSink()
    runner = ScriptedRunner(
        [receipt("Sure, I can help."), receipt(GOOD_CODE, cost=Decimal("0.002"))]
    )
    request = call_request("code_generation")
    result = await build(runner, sink).handle(request)
    assert [r.attempt for r in sink.records] == [1, 2]
    assert sink.records == list(result.attempts)
    assert all(r.request_id == request.request_id for r in sink.records)
    assert sink.records[1].cost_usd == Decimal("0.002")
    assert sink.records[0].caller_lane == "harnessA-1605"


async def test_harness_handler_gate_escalation_cost_a_recorded_dev_attempt_yields_a_cost_row() -> (
    None
):
    """The dev row of a flash attempt that failed (no cost on the row today) still leaves an attempt record."""
    row = recorded_row()
    assert row["backend_id"] == "harness:claude-glm:glm-5.3-flash"
    assert row["cost_usd"] is None
    assert row["terminal_ok"] is False
    sink = ListCostSink()
    runner = ScriptedRunner(
        [
            receipt("", cause="process_failed", wall=row["latency_ms"] / 1000),
            receipt(GOOD_CODE, cost=Decimal("0.001")),
        ]
    )
    result = await build(runner, sink).handle(call_request(row["task_type"]))
    assert len(sink.records) >= 1
    first = sink.records[0]
    assert first.attempt == 1
    assert first.model_name == row["model_name"]
    assert first.wall_seconds == pytest.approx(row["latency_ms"] / 1000)
    assert first.exit_cause == "process_failed"
    assert first.accepted is False
    assert first.gate_kind is None  # no answer, nothing for the gate to judge
    assert result.status == "completed"
    assert len(sink.records) == 2


async def test_harness_handler_gate_escalation_cost_every_rung_rejected_is_rejected_with_every_attempt_recorded() -> (
    None
):
    sink = ListCostSink()
    runner = ScriptedRunner([receipt("no"), receipt("still no"), receipt("nope")])
    result = await build(runner, sink).handle(call_request("code_generation"))
    assert result.status == "rejected"
    assert result.content == ""
    assert len(sink.records) == len(runner.ran) == len(result.attempts)
    assert all(not r.accepted for r in sink.records)


async def test_harness_handler_gate_escalation_cost_a_failing_sink_does_not_fail_the_call() -> (
    None
):
    class BrokenSink:
        def record(self, attempt: object) -> None:
            raise RuntimeError("sink down")

    runner = ScriptedRunner([receipt(GOOD_CODE)])
    result = await HandlerHarnessBackendCall(
        runner=runner,
        gate=HandlerHarnessAnswerGate(),
        cost_sink=BrokenSink(),
        overlay=load_overlay(),
    ).handle(call_request("code_generation"))
    assert result.status == "completed"
