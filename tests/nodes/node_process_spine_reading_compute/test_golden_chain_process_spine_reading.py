# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of piece 7's hourly stop drill (OMN-22943 AC3).

Golden chain: a drill on a sound test instance commits a stop, the lane sent above it is refused, the control lane is
admitted; the run chain is a start and a success, the planted fault (an instance that accepts the drill's lane) is its
own drill chain that ends in a typed failure, and the ledger takes a WORKING row (drill=no) and a NOT_WORKING row
(drill=yes).

Error chain: an instance that accepts the drill's lane ends the run chain in the typed failure
``instance_accepted_drill_lane`` and a NOT_WORKING row; a drill that cannot plant its fault ends in
``planted_fault_not_red``; a ledger that refuses the rows is a typed error and the chain still ends; a replayed run id
starts no second chain.

Named mutations, each of which turns its chain test red: ``gate_ignores_stop`` (every instance the drill builds admits
a lane above a stop) turns the golden chain red; ``fault_not_planted`` (the planted instance is the sound one) turns the
error chain red.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_stop_drill_effect.adapters import build_test_instance
from omnimarket.nodes.node_stop_drill_effect.handlers import (
    HandlerStopDrill,
    StopDrillLedgerAppendError,
)
from omnimarket.nodes.node_stop_drill_effect.models import (
    ModelStopDrillOverlay,
    ModelStopDrillRequest,
    ModelStopDrillResult,
)
from tests.nodes.node_stop_drill_effect.support import (
    LANE,
    NOW,
    RecordingAppender,
    overlay,
)

pytestmark = pytest.mark.unit

CHAIN_NODE = "node_stop_drill_effect:run"
PLANTED_NODE = CHAIN_NODE + ":planted_failure"
REQUEST = ModelStopDrillRequest(run_id="run-1", requested_at=NOW, trigger="test")

Factory = Callable[..., HandlerGateStoreJoinJ6]


def sound(ov: ModelStopDrillOverlay, *, accepting: bool) -> HandlerGateStoreJoinJ6:
    return build_test_instance(ov, accepting=accepting)


def gate_ignores_stop(
    ov: ModelStopDrillOverlay, *, accepting: bool
) -> HandlerGateStoreJoinJ6:
    """Mutation ``gate_ignores_stop``: whichever instance the drill asks for, it admits a lane above a stop."""
    return build_test_instance(ov, accepting=True)


def fault_not_planted(
    ov: ModelStopDrillOverlay, *, accepting: bool
) -> HandlerGateStoreJoinJ6:
    """Mutation ``fault_not_planted``: the instance meant to accept the lane is the sound one."""
    return build_test_instance(ov, accepting=False)


class Run:
    def __init__(
        self, factory: Factory, appender: RecordingAppender | None = None
    ) -> None:
        self.sink = InMemoryEmissionSink()
        self.appender = appender if appender is not None else RecordingAppender()
        self.handler = HandlerStopDrill(
            overlay(),
            appender=self.appender,
            host="h053",
            sink=self.sink,
            instance_factory=factory,
            now=lambda: NOW,
        )

    def go(self) -> ModelStopDrillResult:
        return self.handler.handle(REQUEST)

    def phases(self, node: str) -> list[str]:
        return [e.phase for e in self.sink.events if e.node == node]


def scenario_golden(factory: Factory) -> None:
    """The golden chain, as assertions; the mutation test runs it against a broken drill."""
    run = Run(factory)
    result = run.go()
    assert result.verdict == "WORKING", result.reason
    assert result.planted_red
    assert run.phases(CHAIN_NODE) == ["start", "success"]
    assert run.phases(PLANTED_NODE) == ["start", "failure"]
    planted = [e for e in run.sink.events if e.node == PLANTED_NODE]
    assert all(e.drill for e in planted)
    assert planted[-1].failure_code == "planted_instance_accepts_lane_red"
    assert all(e.piece == 7 for e in run.sink.events)
    assert len(run.appender.rows) == 2
    golden = evidence_row_from_text(run.appender.rows[0])
    error = evidence_row_from_text(run.appender.rows[1])
    assert golden is not None
    assert error is not None
    assert golden.lane == LANE
    assert golden.cells["verdict"] == "WORKING"
    assert golden.cells["drill"] == "no"
    assert golden.cells["piece"] == "7"
    assert golden.cells["max_age"] == "7200"
    assert "probed_at" in golden.cells
    assert "published_at" in golden.cells
    assert error.cells["verdict"] == "NOT_WORKING"
    assert error.cells["drill"] == "yes"
    assert error.cells["reason"] == "planted_instance_accepts_lane_red"


def scenario_error(factory: Factory) -> None:
    """The error chain, as assertions: the drill's own planted fault must read red."""
    run = Run(factory)
    result = run.go()
    assert result.planted_red, "the planted fault did not read red"
    assert result.planted_reading.reasons == ("instance-accepted-drill-lane",)
    assert run.phases(PLANTED_NODE) == ["start", "failure"]
    error = evidence_row_from_text(run.appender.rows[-1])
    assert error is not None
    assert error.cells["drill"] == "yes"


def test_chain_golden_a_sound_instance_refuses_the_lane_and_the_chains_end_as_required() -> (
    None
):
    scenario_golden(sound)


def test_chain_error_the_planted_fault_reads_red_and_is_its_own_drill_chain() -> None:
    scenario_error(sound)


def test_chain_error_an_instance_that_accepts_the_lane_ends_the_run_chain_in_a_typed_failure() -> (
    None
):
    run = Run(gate_ignores_stop)
    result = run.go()
    assert result.verdict == "NOT_WORKING"
    assert result.reason == "instance-accepted-drill-lane"
    assert run.phases(CHAIN_NODE) == ["start", "failure"]
    failure = [e for e in run.sink.events if e.node == CHAIN_NODE][-1]
    assert failure.failure_code == "instance_accepted_drill_lane"
    row = evidence_row_from_text(run.appender.rows[0])
    assert row is not None
    assert row.cells["verdict"] == "NOT_WORKING"
    assert row.cells["drill"] == "no"
    assert row.cells["reason"] == "instance-accepted-drill-lane"


def test_chain_error_a_drill_that_cannot_plant_its_fault_is_a_typed_failure() -> None:
    run = Run(fault_not_planted)
    result = run.go()
    assert not result.planted_red
    assert result.verdict == "NOT_WORKING"
    assert result.reason == "planted-fault-not-red"
    failure = [e for e in run.sink.events if e.node == CHAIN_NODE][-1]
    assert failure.failure_code == "planted_fault_not_red"
    assert run.phases(PLANTED_NODE) == []
    assert len(run.appender.rows) == 1


@pytest.mark.parametrize("status", ["refused", "error", "pending"])
def test_chain_error_a_ledger_that_does_not_take_the_rows_is_a_typed_error(
    status: str,
) -> None:
    run = Run(sound, RecordingAppender(status))
    with pytest.raises(StopDrillLedgerAppendError):
        run.go()
    # The chains still ended: a start with no terminal reads as a hung run.
    assert run.phases(CHAIN_NODE) == ["start", "success"]


def test_chain_golden_a_replayed_run_id_starts_no_second_chain() -> None:
    run = Run(sound)
    run.go()
    run.go()
    assert run.phases(CHAIN_NODE) == ["start", "success"]
    assert run.phases(PLANTED_NODE) == ["start", "failure"]
    assert len(run.appender.rows) == 2


def test_chain_mutation_gate_ignores_stop_turns_the_golden_chain_red() -> None:
    with pytest.raises(AssertionError):
        scenario_golden(gate_ignores_stop)


def test_chain_mutation_fault_not_planted_turns_the_error_chain_red() -> None:
    with pytest.raises(AssertionError):
        scenario_error(fault_not_planted)


NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"


@pytest.mark.parametrize(
    ("node", "terminal"),
    [
        (
            "node_process_spine_reading_compute",
            "onex.evt.omnimarket.process-spine-read.v1",
        ),
        ("node_stop_drill_effect", "onex.evt.omnimarket.stop-drill-completed.v1"),
    ],
)
def test_chain_each_node_declares_the_terminal_event_its_run_ends_in(
    node: str, terminal: str
) -> None:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert contract["terminal_event"] == terminal
    assert terminal in contract["event_bus"]["publish_topics"]
    assert terminal in contract["externally_consumed_topics"]
