# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests for node_delegation_synthetic_probe_compute (OMN-22977).

The request travels on the contract's command topic of the in-memory bus, the node judges it and the
judged event goes out on the contract's terminal topic, with one correlation id. A task that came
back with its result is the golden chain; a task whose result was dropped is the error chain, a typed
FAIL with its reason and a row the reading reads, never silence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_delegation_synthetic_probe_compute.handlers.handler_delegation_synthetic_probe import (
    HandlerDelegationSyntheticProbe,
)
from omnimarket.nodes.node_delegation_synthetic_probe_compute.models.model_delegation_synthetic_probe import (
    EnumSyntheticProbeReason,
    EnumSyntheticProbeVerdict,
    ModelSyntheticProbeRequest,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain, assert_error_chain

pytestmark = pytest.mark.unit

CONTRACT = yaml.safe_load(
    (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_delegation_synthetic_probe_compute/contract.yaml"
    ).read_text(encoding="utf-8")
)
COMMAND_TOPIC = CONTRACT["runtime_dispatch"]["command_topic"]
TERMINAL_TOPIC = CONTRACT["terminal_event"]
ISSUED = datetime(2026, 11, 7, 9, 5, tzinfo=UTC)


async def run_chain(result_text: str | None) -> tuple[str, ChainRecorder]:
    cid = str(uuid4())
    request = ModelSyntheticProbeRequest(
        correlation_id=cid,
        task_id="synthetic-golden-chain",
        issued_at=ISSUED,
        observed_at=ISSUED + timedelta(seconds=12),
        terminal_event_type="ModelDelegationCompleted",
        result_text=result_text,
        cost_usd=0.0012,
    )
    recorder = ChainRecorder(EventBusInmemory(environment="test", group=cid))
    await recorder.publish(COMMAND_TOPIC, request, correlation_id=cid)
    result = HandlerDelegationSyntheticProbe().handle(request)
    await recorder.publish(TERMINAL_TOPIC, result, correlation_id=cid, causation_id=cid)
    return cid, recorder


async def test_golden_chain_a_task_with_its_result_and_cost_is_judged_pass() -> None:
    cid, recorder = await run_chain("### ANSWER\npong")
    assert_chain(
        recorder.events,
        expected_event_types=[
            "ModelSyntheticProbeRequest",
            "ModelSyntheticProbeResult",
        ],
        terminal_fields={
            "verdict": EnumSyntheticProbeVerdict.PASS,
            "reason": EnumSyntheticProbeReason.OK,
            "cost_usd": 0.0012,
        },
        correlation_id=cid,
        bus_history_count=await recorder.bus_history_count(),
    )
    assert recorder.events[-1].topic == TERMINAL_TOPIC


async def test_error_chain_a_task_with_its_result_dropped_is_judged_fail_with_a_row() -> (
    None
):
    cid, recorder = await run_chain("")
    assert_error_chain(
        recorder.events,
        expected_event_types=[
            "ModelSyntheticProbeRequest",
            "ModelSyntheticProbeResult",
        ],
        terminal_fields={
            "verdict": EnumSyntheticProbeVerdict.FAIL,
            "failure_reason": "result_dropped",
        },
        correlation_id=cid,
        bus_history_count=await recorder.bus_history_count(),
    )
    terminal = recorder.events[-1].payload
    assert terminal.row_cells["verdict"] == "FAIL"  # type: ignore[attr-defined]
    assert terminal.row_cells["result"] == "dropped"  # type: ignore[attr-defined]
