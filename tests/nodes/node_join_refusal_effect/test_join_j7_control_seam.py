# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of join J7's failing control (OMN-19998): the chains the J7 runner writes are the chains the floor's fold reads.

The producer side is the real J7 runner (``HandlerLabCheckRunJoinJ7Run``) over the real in-memory gate store, stop gate and
lab check effect, with the example overlay's ``control`` block. The consumer side is the floor's own receipt fold
(``HandlerFloorReceipt``) over the journal the runner wrote. Neither side is stubbed: the test changes the shape of the
producer's chains and reads what the consumer does.

* golden: the control writes piece 7's typed failure under its own id (no drill) and piece 8's one typed ``upstream_`` refusal
  that never started; the fold reads J7 as ``error_only`` with no finding;
* error: a control the stop gate still refuses (it broke nothing) writes no chain, so no error side;
* control: a planted rename of the correlation id, a refusal that starts, and a drill flag on piece 7's failure each change
  what the fold reads;
* parity: the same chains cross a bus (in-memory always, Redpanda when ``JOIN_J7_PARITY_BOOTSTRAP`` names a lab broker) and
  the fold reads the same join state as an in-process read.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
    ModelPieceEventRule,
)
from omnimarket.models.model_lab_check_run_join_j7_run import (
    EnumJoinJ7RunOutcome,
    ModelJoinJ7RunCommand,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_lab_check_run_effect import runtime_join_j7
from omnimarket.nodes.node_lab_check_run_effect.handlers import (
    handler_lab_check_run_join_j7_run as runner_module,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_join_j7_run import (
    HandlerLabCheckRunJoinJ7Run,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay_join_j7 import (
    ModelJoinJ7Overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_join_refusal_effect import test_join_j7_seam as bus_seam

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_check_run_effect/overlay_join_j7.example.yaml"
)
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
OVERLAY = ModelJoinJ7Overlay.model_validate(
    {
        **yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")),
        "allow_injected_break": True,
    }
)
FROM_PIECE, TO_PIECE = 7, 8
RULE = ModelPieceEventRule.model_validate(
    {
        "piece": FROM_PIECE,
        "node": "example_stop_drill_node:run",
        "golden": {
            "row_type": "STATUS",
            "lane": "example-stop-drill",
            "cells": {"src": "example-stop-drill", "drill": "no", "verdict": "WORKING"},
        },
        "error": {
            "row_type": "STATUS",
            "lane": "example-stop-drill",
            "cells": {"src": "example-stop-drill", "verdict": "NOT_WORKING"},
        },
        "failure_code_cell": "reason",
        "failure_default": "stop_drill_untyped",
        "error_drill_cell": "drill",
        "lookback_days": 7,
    }
)
CODE = OVERLAY.control.failure_code
EXPECTED = {
    "events": [
        [FROM_PIECE, "start", None, False],
        [FROM_PIECE, "failure", CODE, False],
        [TO_PIECE, "failure", f"upstream_{CODE}", False],
    ],
    "findings": [],
    "state": "error_only",
}


class _Clock:
    def __init__(self) -> None:
        self._t = NOW

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def written_control() -> list[dict[str, Any]]:
    """The control's chains as the real runner writes them, as the JSON a bus carries."""
    sink = InMemoryEmissionSink()
    result = HandlerLabCheckRunJoinJ7Run(
        source=InMemoryLedger([]),
        sink=sink,
        upstream=RULE,
        overlay=OVERLAY,
        golden_rig=lambda: runtime_join_j7.build_rig(OVERLAY),
        drill_rig=lambda: runtime_join_j7.build_rig(OVERLAY),
        window_hours=48,
        to_piece=TO_PIECE,
        backend="in_memory",
        clock=_Clock(),
    ).handle(ModelJoinJ7RunCommand())
    control = [i for i in result.items if i.control]
    assert [i.outcome for i in control] == [EnumJoinJ7RunOutcome.CONTROL_RECORDED]
    return [
        e.model_dump(mode="json")
        for e in sink.events
        if e.correlation_id == control[0].correlation_id
    ]


def fold(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer: the emissions from the message into the floor's receipt fold, one join read."""
    emissions = tuple(ModelNodeEmission.model_validate(e) for e in payload["emissions"])
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(hours=1),
            emissions=emissions,
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (FROM_PIECE, TO_PIECE)
            ),
            joins=(
                ModelJoinDef(
                    id=OVERLAY.join_id,
                    from_piece=FROM_PIECE,
                    to_piece=TO_PIECE,
                    label="process spine to CI",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=900,
        )
    )
    return {
        "id": payload["id"],
        "events": sorted(
            ([e.piece, e.phase, e.failure_code, e.drill] for e in emissions), key=str
        ),
        "findings": [f.code for f in receipts.findings],
        "state": next(j for j in receipts.joins if j.join == OVERLAY.join_id).state,
    }


def refuse_unreadable(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer on a bus: an emission it cannot read is answered as refused, never silently dropped."""
    try:
        return fold(payload)
    except ValueError as exc:
        return {"id": payload["id"], "refused": type(exc).__name__}


def request(emissions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": os.urandom(8).hex(), "emissions": emissions}


def settled(reply: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in reply.items() if k != "id"}


def expected_events() -> list[list[Any]]:
    return sorted(EXPECTED["events"], key=str)


def read(emissions: list[dict[str, Any]]) -> dict[str, Any]:
    reply = settled(fold(request(emissions)))
    reply["events"] = sorted(reply["events"], key=str)
    return reply


def test_golden_the_control_is_a_non_drill_piece_7_failure_answered_by_piece_8_typed_refusal() -> (
    None
):
    reply = read(written_control())

    assert reply == {**EXPECTED, "events": expected_events()}


def test_golden_the_control_chains_share_one_id_that_is_the_controls_own() -> None:
    emissions = written_control()

    ids = {e["correlation_id"] for e in emissions}
    assert len(ids) == 1
    assert ids.pop().startswith(OVERLAY.control.id_prefix)


def test_error_a_control_the_stop_gate_still_refuses_writes_no_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = runner_module.stop_gate

    def enforcing(request: Any, fsm: Any) -> Any:
        modes = {k: type(v)("enforce") for k, v in request.policy.modes.items()}
        policy = request.policy.model_copy(update={"modes": modes})
        return real(request.model_copy(update={"policy": policy}), fsm)

    monkeypatch.setattr(runner_module, "stop_gate", enforcing)
    sink = InMemoryEmissionSink()
    result = HandlerLabCheckRunJoinJ7Run(
        source=InMemoryLedger([]),
        sink=sink,
        upstream=RULE,
        overlay=OVERLAY,
        golden_rig=lambda: runtime_join_j7.build_rig(OVERLAY),
        drill_rig=lambda: runtime_join_j7.build_rig(OVERLAY),
        window_hours=48,
        to_piece=TO_PIECE,
        backend="in_memory",
        clock=_Clock(),
    ).handle(ModelJoinJ7RunCommand())

    control = [i for i in result.items if i.control]
    assert [i.outcome for i in control] == [EnumJoinJ7RunOutcome.CONTROL_NOT_BROKEN]
    assert result.ok is False
    assert [
        e for e in sink.events if e.correlation_id == control[0].correlation_id
    ] == []


def test_control_a_planted_rename_of_the_correlation_id_cell_is_refused_by_the_consumer() -> (
    None
):
    emissions = [
        {("cid" if k == "correlation_id" else k): v for k, v in e.items()}
        for e in written_control()
    ]

    with pytest.raises(ValidationError):
        fold(request(emissions))


def test_control_a_refusal_that_starts_turns_the_join_broken() -> None:
    emissions = written_control()
    refusal = next(e for e in emissions if e["piece"] == TO_PIECE)
    started = {**refusal, "phase": "start", "failure_code": None, "failure_detail": ""}

    reply = read([started, *emissions])

    assert reply["state"] == "broken"
    assert reply["findings"] == ["downstream_started_after_upstream_failed"]


def test_control_a_drill_flag_on_piece_7s_failure_hides_the_error_side() -> None:
    emissions = [{**e, "drill": e["piece"] == FROM_PIECE} for e in written_control()]

    assert read(emissions)["state"] != "error_only"


def test_control_the_unplanted_chains_pass_the_same_expectation() -> None:
    assert read(written_control())["state"] == "error_only"


@pytest.mark.join_j7_parity
def test_parity_in_memory_bus_reads_what_the_in_process_fold_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bus_seam, "consume", fold)
    emissions = written_control()

    reply = asyncio.run(_over(bus_seam.InmemoryBus(), emissions))

    assert read_reply(reply) == read(emissions)


@pytest.mark.join_j7_parity
def test_parity_a_renamed_field_crossing_the_in_memory_bus_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bus_seam, "consume", refuse_unreadable)
    emissions = [{**e, "phase": "done"} for e in written_control()]

    reply = asyncio.run(_over(bus_seam.InmemoryBus(), emissions))

    assert settled(reply) == {"refused": "ValidationError"}


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.join_j7_parity
def test_parity_redpanda_bus_reads_what_the_in_memory_bus_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get(bus_seam.BOOTSTRAP_ENV):
        pytest.skip(
            f"Kafka not reachable: {bus_seam.BOOTSTRAP_ENV} names no lab broker"
        )
    monkeypatch.setattr(bus_seam, "consume", fold)
    emissions = written_control()

    broker = asyncio.run(
        _over(bus_seam.RedpandaBus(os.environ[bus_seam.BOOTSTRAP_ENV]), emissions)
    )
    memory = asyncio.run(_over(bus_seam.InmemoryBus(), emissions))

    assert read_reply(broker) == read_reply(memory) == read(emissions)


async def _over(bus: bus_seam.Bus, emissions: list[dict[str, Any]]) -> dict[str, Any]:
    await bus.start()
    try:
        return await bus.call(request(emissions))
    finally:
        await bus.stop()


def read_reply(reply: dict[str, Any]) -> dict[str, Any]:
    out = settled(reply)
    out["events"] = sorted(out["events"], key=str)
    return out
