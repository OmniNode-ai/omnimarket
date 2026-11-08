# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 15: the hourly synthetic delegated task returns a result and a cost, and dropping the result flips the reading (OMN-22977).

The task goes through the REAL delegation orchestrator on the in-memory bus (the same handler
the chain tests drive). Its terminal event is judged by node_delegation_synthetic_probe_compute,
which renders the ledger row the piece 15 reading selects, and node_reading_compute reads those
rows over closed hourly windows exactly as the reading writer does. Nothing in the path is a
double except the in-memory bus.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.models.model_reading import (
    EnumCellOp,
    EnumReadingVerdict,
    EnumSourceMode,
    ModelCellPredicate,
    ModelLedgerEvidenceRow,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingSource,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_synthetic_probe_compute.handlers.handler_delegation_synthetic_probe import (
    HandlerDelegationSyntheticProbe,
)
from omnimarket.nodes.node_delegation_synthetic_probe_compute.models.model_delegation_synthetic_probe import (
    EnumSyntheticProbeReason,
    EnumSyntheticProbeVerdict,
    ModelSyntheticProbeRequest,
    ModelSyntheticProbeResult,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
)
from tests.chains.delegation import _builders as b
from tests.chains.delegation.conftest import pinned_bifrost_contract

__all__ = ["pinned_bifrost_contract"]

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("pinned_bifrost_contract", "stub_provider_quota_reader"),
]

PROBE = HandlerDelegationSyntheticProbe()
RULES = PROBE.rules
READING = HandlerReadingCompute()
HOUR = timedelta(seconds=RULES.cadence_seconds)
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
# The writer reads a closed window a short while after it ends.
CLOSE_DELAY = timedelta(seconds=60)

SPEC = ModelPieceReadingSpec(
    piece=RULES.row_piece,
    name="delegation-chain",
    readable_today=True,
    basis="synthetic-hourly-task",
    sources=(
        ModelReadingSource(
            source_id="synthetic-task",
            row_type="STATUS",
            lane=RULES.row_lane,
            cells={"src": RULES.row_source, "piece": str(RULES.row_piece)},
            mode=EnumSourceMode.COUNT,
            pass_when=(
                ModelCellPredicate(cell="verdict", op=EnumCellOp.EQ, value="PASS"),
            ),
            min_rows=1,
            # The bound of the reading is the cadence: one task per hour must pass in the window.
            max_age_seconds=RULES.cadence_seconds,
        ),
    ),
    injected_fault="Drop the result of the synthetic task in a test route",
)


async def run_synthetic_task(
    *, drop_result: bool = False, answer: str = "pong"
) -> tuple[UUID, tuple[b.ChainEvent, ...]]:
    """Drive the synthetic task through the real orchestrator to its terminal events."""

    def issue(h: b.HandlerDelegationWorkflow, c: UUID) -> list:
        return h.handle_delegation_request(
            ModelDelegationRequest(
                prompt=RULES.prompt,
                task_type="test",
                correlation_id=c,
                emitted_at=datetime.now(UTC),
                tenant_id=b.TENANT,
            )
        )

    def infer(h: b.HandlerDelegationWorkflow, c: UUID) -> list:
        response = b.inference_ok(c).model_copy(
            update={"content": "### ANSWER\n" + answer}
        )
        return h.handle_inference_response(response)

    cid, run = await b.drive(
        [
            issue,
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="free_local")
            ),
            infer,
            lambda h, c: h.handle_gate_result(b.gate_result(c, passed=True)),
        ]
    )
    terminal = next(
        e for e in run.events if type(e.payload).__name__ == "ModelDelegationCompleted"
    )
    # The test route: the terminal as the synthetic task's observer saw it, with the result
    # and the cost it carried. The drop is injected here, on the observed terminal.
    payload = terminal.payload
    assert isinstance(payload.content, str)
    content = "" if drop_result else payload.content
    observed = terminal.__class__(
        topic=terminal.topic,
        event_type=terminal.event_type,
        correlation_id=terminal.correlation_id,
        causation_id=terminal.causation_id,
        payload=payload.model_copy(update={"content": content}),
    )
    events = tuple(observed if e is terminal else e for e in run.events)
    return cid, events


def judge(
    cid: UUID,
    events: tuple[b.ChainEvent, ...],
    *,
    issued_at: datetime,
    elapsed: timedelta = timedelta(seconds=12),
    cost: float | None = 0.0012,
    probe: HandlerDelegationSyntheticProbe = PROBE,
) -> ModelSyntheticProbeResult:
    terminal = next(
        (e for e in events if type(e.payload).__name__ == "ModelDelegationCompleted"),
        None,
    )
    return probe.handle(
        ModelSyntheticProbeRequest(
            correlation_id=str(cid),
            task_id=f"synthetic-{cid.hex[:12]}",
            issued_at=issued_at,
            observed_at=None if terminal is None else issued_at + elapsed,
            terminal_event_type=None if terminal is None else terminal.event_type,
            result_text=None if terminal is None else terminal.payload.content,
            cost_usd=cost,
        )
    )


def as_row(result: ModelSyntheticProbeResult) -> ModelLedgerEvidenceRow:
    return ModelLedgerEvidenceRow(
        stamp=result.row_stamp,
        row_type="STATUS",
        lane=RULES.row_lane,
        cells={**result.row_cells, "req": str(uuid4())},
        raw=result.row_text,
    )


def read_window(
    rows: list[ModelLedgerEvidenceRow], end: datetime
) -> EnumReadingVerdict:
    return READING.handle(
        ModelReadingComputeRequest(
            spec=SPEC,
            window_start=end - HOUR,
            window_end=end,
            cutoff=end + CLOSE_DELAY,
            order_hash="o",
            overlay_hash="v",
            revision="1.0.0",
            sources=(
                ModelReadingSourceRows(source_id="synthetic-task", rows=tuple(rows)),
            ),
        )
    ).verdict


async def test_the_synthetic_task_returns_a_result_and_a_cost() -> None:
    cid, events = await run_synthetic_task()
    result = judge(cid, events, issued_at=T0 + timedelta(minutes=5))
    assert result.verdict is EnumSyntheticProbeVerdict.PASS
    assert result.reason is EnumSyntheticProbeReason.OK
    assert result.cost_usd is not None
    assert result.row_cells["result"] == "present"
    assert result.row_cells["cost_usd"] == "0.001200"
    assert result.row_cells["elapsed_s"] == "12"
    assert [e.event_type for e in events][-2:] == [
        "ModelDelegationCompleted",
        "ModelDelegationTerminalCompletedV2",
    ]


async def test_one_passing_task_per_hour_reads_working_window_after_window() -> None:
    rows: list[ModelLedgerEvidenceRow] = []
    for hour in range(3):
        cid, events = await run_synthetic_task()
        rows.append(
            as_row(
                judge(cid, events, issued_at=T0 + hour * HOUR + timedelta(minutes=5))
            )
        )
        assert read_window(rows, T0 + (hour + 1) * HOUR) is EnumReadingVerdict.WORKING


async def test_drop_result_flips_the_reading_within_one_tick_of_its_bound() -> None:
    # Hour 0 passes; hour 1 issues the same task through the same route with its result dropped.
    good_cid, good_events = await run_synthetic_task()
    good = as_row(judge(good_cid, good_events, issued_at=T0 + timedelta(minutes=5)))
    assert read_window([good], T0 + HOUR) is EnumReadingVerdict.WORKING

    dropped_cid, dropped_events = await run_synthetic_task(drop_result=True)
    dropped_result = judge(
        dropped_cid, dropped_events, issued_at=T0 + HOUR + timedelta(minutes=5)
    )
    assert dropped_result.verdict is EnumSyntheticProbeVerdict.FAIL
    assert dropped_result.reason is EnumSyntheticProbeReason.RESULT_DROPPED
    assert dropped_result.row_cells["result"] == "dropped"
    rows = [good, as_row(dropped_result)]
    # The very next closed window reads NOT WORKING, the one tick after the drop.
    assert read_window(rows, T0 + 2 * HOUR) is EnumReadingVerdict.NOT_WORKING
    # The window before the drop is untouched: the reading flipped because of the drop.
    assert read_window(rows, T0 + HOUR) is EnumReadingVerdict.WORKING


async def test_drop_result_flips_the_reading_when_no_terminal_comes_back() -> None:
    cid, events = await run_synthetic_task()
    no_terminal = tuple(e for e in events if e.event_type != "ModelDelegationCompleted")
    result = judge(cid, no_terminal, issued_at=T0 + timedelta(minutes=5))
    assert result.reason is EnumSyntheticProbeReason.NO_TERMINAL
    assert read_window([as_row(result)], T0 + HOUR) is EnumReadingVerdict.NOT_WORKING


async def test_drop_result_flips_the_reading_when_the_probe_row_is_never_written() -> (
    None
):
    cid, events = await run_synthetic_task()
    good = as_row(judge(cid, events, issued_at=T0 + timedelta(minutes=5)))
    assert read_window([good], T0 + HOUR) is EnumReadingVerdict.WORKING
    # No row in the next hour at all: the stale source reads NOT WORKING, never silence.
    assert read_window([good], T0 + 2 * HOUR) is EnumReadingVerdict.NOT_WORKING


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"elapsed": timedelta(seconds=RULES.bound_seconds + 1)}, "over_bound"),
        ({"cost": None}, "cost_missing"),
    ],
)
async def test_each_other_way_to_fail_has_its_own_reason(
    kwargs: dict[str, Any], reason: str
) -> None:
    cid, events = await run_synthetic_task()
    result = judge(cid, events, issued_at=T0, **kwargs)
    assert result.verdict is EnumSyntheticProbeVerdict.FAIL
    assert result.reason.value == reason
    assert read_window([as_row(result)], T0 + HOUR) is EnumReadingVerdict.NOT_WORKING


async def test_a_wrong_answer_fails_with_its_own_reason() -> None:
    cid, events = await run_synthetic_task(answer="something else")
    result = judge(cid, events, issued_at=T0)
    assert result.reason is EnumSyntheticProbeReason.WRONG_ANSWER


async def test_drop_result_flips_control_a_probe_that_ignores_the_result_does_not_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing control: break the probe on purpose and the flip test above would be red."""

    def ignore_the_result(
        self: HandlerDelegationSyntheticProbe,
        request: ModelSyntheticProbeRequest,
        elapsed: int | None,
    ) -> EnumSyntheticProbeReason:
        return EnumSyntheticProbeReason.OK

    monkeypatch.setattr(HandlerDelegationSyntheticProbe, "_reason", ignore_the_result)
    cid, events = await run_synthetic_task(drop_result=True)
    result = judge(cid, events, issued_at=T0 + timedelta(minutes=5))
    assert result.verdict is EnumSyntheticProbeVerdict.PASS  # the break hides the drop
    assert read_window([as_row(result)], T0 + HOUR) is EnumReadingVerdict.WORKING


async def test_the_whole_flow_request_to_result_cost_row_and_reading() -> None:
    """One whole-flow run: request, routing, inference, gate, terminal, probe row, reading."""
    cid, events = await run_synthetic_task()
    assert [e.event_type for e in events] == [
        "ModelRoutingIntent",
        "ModelInferenceIntent",
        "ModelQualityGateIntent",
        "ModelDelegationCompleted",
        "ModelDelegationTerminalCompletedV2",
    ]
    assert {str(e.correlation_id) for e in events} == {str(cid)}
    result = judge(cid, events, issued_at=T0 + timedelta(minutes=5))
    assert result.verdict is EnumSyntheticProbeVerdict.PASS
    assert read_window([as_row(result)], T0 + HOUR) is EnumReadingVerdict.WORKING
