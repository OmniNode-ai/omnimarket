# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests for the code-eval trial columns (OMN-23628).

Pins the closure loop for the new trial columns: a context-ROI run-completed
event carrying try/control rows projects into ``context_roi_scores`` and the
applied projection result is published on the contract's terminal topic. The
error chain proves a malformed row (bad ``size_band`` / ``row_kind``) raises a
pydantic ``ValidationError``, writes no row, and is dead-lettered onto the
contract's declared DLQ topic carrying the failure reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel, ValidationError

from omnimarket.events.context_roi import (
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.models.context_roi_trial_fields import EnumContextRoiRowKind
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
    ModelContextRoiRunCompletedEvent,
    ModelProjectionResult,
)
from omnimarket.projection.dlq import dlq_topics_from_contract, route_to_dlq
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.chains.chain_assert import ChainRecorder, assert_chain, assert_error_chain

pytestmark = pytest.mark.unit

HANDLER = HandlerProjectionContextRoi()
CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_context_roi/contract.yaml")
RUNNER_TERMINAL_TOPIC = "onex.evt.omnimarket.context-roi-run-completed.v1"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-context-roi-applied.v1"
DLQ_TOPIC = "onex.dlq.omnimarket.projection-context-roi-malformed.v1"


def _contract() -> dict[str, Any]:
    with open(CONTRACT_PATH) as handle:
        return dict(yaml.safe_load(handle))


def _row(
    *,
    correlation_id: str,
    row_kind: EnumContextRoiRowKind = EnumContextRoiRowKind.TRY,
    check_verdict: EnumCodeScoreVerdict | None = None,
    unscorable_source: EnumCodeScoreSource | None = None,
    size_band: str = "S",
    try_ordinal: int = 0,
) -> ModelAttemptReductionRow:
    return ModelAttemptReductionRow(
        run_id="run-001",
        correlation_id=correlation_id,
        task_id="task-A",
        run_order=1,
        context_factor_subset="golden_exemplar",
        context_pack_hash="abc123",
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=100,
        completion_tokens=50,
        estimated_cost=0.0,
        model_id="qwen3-coder-30b",
        provider="local",
        endpoint_ref="local-coder",
        row_kind=row_kind,
        check_verdict=check_verdict,
        unscorable_source=unscorable_source,
        size_band=size_band,
        try_ordinal=try_ordinal,
    )


class _MalformedInput(BaseModel):
    """The malformed run payload that must be rejected by the projection."""

    correlation_id: str
    rows: list[dict[str, Any]]


class _DeadLetter(BaseModel):
    """The dead-letter envelope the runtime publishes for a malformed row."""

    correlation_id: str
    failure_reason: str


@pytest.mark.asyncio
async def test_golden_chain_projects_trial_columns_and_publishes_applied() -> None:
    """A run-completed event with try + control rows projects and publishes."""
    bus = EventBusInmemory()
    recorder = ChainRecorder(bus)
    db = InmemoryDatabaseAdapter()

    try_row = _row(
        correlation_id="row-try-1",
        row_kind=EnumContextRoiRowKind.TRY,
        check_verdict=EnumCodeScoreVerdict.PASS,
        size_band="S",
        try_ordinal=1,
    )
    control_row = _row(
        correlation_id="row-control-1",
        row_kind=EnumContextRoiRowKind.CONTROL,
    )
    result = ModelContextRoiRunResult(run_id="run-001", rows=(try_row, control_row))
    event = ModelContextRoiRunCompletedEvent(
        **result.model_dump(mode="python", include={"run_id", "rows"})
    )
    await recorder.publish(RUNNER_TERMINAL_TOPIC, event, correlation_id="run-chain-1")

    payload: dict[str, object] = {**event.model_dump(mode="json"), "_db": db}
    out = HANDLER.handle(payload)
    projection = ModelProjectionResult(
        rows_upserted=out["rows_upserted"],
        table=out["table"],
    )
    await recorder.publish(APPLIED_TOPIC, projection, correlation_id="run-chain-1")

    projection_rows = db.query("context_roi_scores")
    assert_chain(
        recorder.events,
        expected_event_types=[
            "ModelContextRoiRunCompletedEvent",
            "ModelProjectionResult",
        ],
        terminal_fields={"rows_upserted": 2, "table": "context_roi_scores"},
        correlation_id="run-chain-1",
        projection_rows=projection_rows,
        expected_projection_rows=[
            {
                "correlation_id": "row-control-1",
                "row_kind": "control",
            },
            {
                "correlation_id": "row-try-1",
                "row_kind": "try",
                "size_band": "S",
                "check_verdict": "pass",
            },
        ],
        ordering_key="correlation_id",
        bus_history_count=2,
    )


@pytest.mark.asyncio
async def test_error_chain_dead_letters_malformed_row() -> None:
    """A malformed row raises ValidationError, writes no row, and dead-letters."""
    bus = EventBusInmemory()
    recorder = ChainRecorder(bus)
    db = InmemoryDatabaseAdapter()
    contract = _contract()
    dlq_topics = dlq_topics_from_contract(contract)
    assert dlq_topics == [DLQ_TOPIC]

    malformed_rows = [
        {"correlation_id": "run-chain-bad", "row_kind": "bogus", "size_band": "XL"}
    ]
    message: dict[str, Any] = {"run_id": "run-bad-001", "rows": malformed_rows}
    await recorder.publish(
        RUNNER_TERMINAL_TOPIC,
        _MalformedInput(correlation_id="run-chain-bad", rows=malformed_rows),
        correlation_id="run-chain-bad",
    )

    with pytest.raises(ValidationError) as raised:
        HANDLER.handle({**message, "_db": db})
    assert db.query("context_roi_scores") == [], "no row may be written on failure"
    failure_reason = str(raised.value)

    published: list[tuple[str, bytes]] = []

    async def _publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    routed = await route_to_dlq(
        publish=_publish,
        dlq_topics=dlq_topics,
        original_message=message,
        failure_reason=failure_reason,
        handler="HandlerProjectionContextRoi",
        correlation_id="run-chain-bad",
    )
    assert routed is True
    assert [topic for topic, _ in published] == [DLQ_TOPIC]
    envelope = json.loads(published[0][1])
    assert envelope["handler"] == "HandlerProjectionContextRoi"
    assert envelope["original_message"] == message

    await recorder.publish(
        DLQ_TOPIC,
        _DeadLetter(
            correlation_id=envelope["correlation_id"],
            failure_reason=envelope["failure_reason"],
        ),
        correlation_id="run-chain-bad",
    )
    assert_error_chain(
        recorder.events,
        expected_event_types=["_MalformedInput", "_DeadLetter"],
        terminal_fields={"failure_reason": failure_reason[:4096]},
        correlation_id="run-chain-bad",
        bus_history_count=2,
    )


def test_contract_declares_dlq_topic_and_excludes_it_from_publish_subscribe() -> None:
    """The contract's DLQ topic is declared and not a publish/subscribe topic."""
    contract = _contract()
    assert contract["event_bus"]["dlq_topics"] == [DLQ_TOPIC]
    assert DLQ_TOPIC not in contract["event_bus"].get("subscribe_topics", [])
    assert DLQ_TOPIC not in contract["event_bus"].get("publish_topics", [])
    exposures = contract["projection_api"]["exposures"]
    columns = exposures[0]["columns"]
    for column in (
        "check_verdict",
        "unscorable_source",
        "row_kind",
        "try_end",
        "suite_digest",
        "size_band",
        "sequence_id",
        "try_ordinal",
        "diff_hash",
        "max_turns",
        "try_timeout_s",
        "generation_s",
        "env_sync_s",
        "check_s",
    ):
        assert column in columns, f"exposure must list column {column!r}"
