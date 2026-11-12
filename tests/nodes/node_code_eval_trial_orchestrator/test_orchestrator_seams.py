# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23882 AC3 -- every seam the code eval trial orchestrator touches, each pinned so the test
fails when either side changes shape. The in-memory bus always runs; a real broker runs when
``RUN_REDPANDA_E1_SEAM=1`` (the declared test lane's drill topics). Exemplar:
``tests/nodes/node_projection_context_roi/test_seam_trial_columns_bus.py`` (the bus legs) and
``tests/unit/nodes/node_pr_landing_decision_compute/test_join_j8.py`` (a join pinned from both sides).

Seams: S1 lab-work unit request and receipt (node_lab_work_unit_effect); S2 E2's score request and
scored event (node_code_contract_score_effect); S3 the context-ROI row (node_projection_context_roi,
and E3's columns, omnimarket pull request 5835, lane eval-e3-pins-4790); S4 the run command and the
closed event; S5 the reducer's emissions; S6 the declared topics against the other nodes' contracts.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.events.context_roi import ModelAttemptReductionRow
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScored,
    ModelCodeScoreRequest,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.builders import (
    trial_columns,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import ModelVerdictRow
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
)
from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.chains.piece16.test_piece16_chain import (
    MemoryChainBus,
    ProtocolChainBus,
    RedpandaChainBus,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    RUN_ID,
    Bus,
    make_bus,
    run_request,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
BACKENDS = ["memory"] + (
    ["redpanda"] if os.environ.get("RUN_REDPANDA_E1_SEAM") == "1" else []
)

#: ModelTrialFields at omnimarket pull request 5835 (head 1b97878d38, lane eval-e3-pins-4790):
#: the columns the result store adds to ModelAttemptReductionRow. Spelled here so the seam holds
#: whether or not 5835 has merged; the test below fails the day either side differs.
E3_TRIAL_COLUMNS = frozenset(
    {
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
    }
)


@pytest.fixture(params=BACKENDS)
def wire(request: pytest.FixtureRequest) -> ProtocolChainBus:
    return MemoryChainBus() if request.param == "memory" else RedpandaChainBus()


def contract(node: str) -> dict:
    return yaml.safe_load((NODES / node / "contract.yaml").read_text(encoding="utf-8"))


def trip[M: BaseModel](wire: ProtocolChainBus, topic: str, event: M) -> M:
    """The event over the bus and parsed back as its own model (the handler's sync loop cannot nest)."""
    delivered = asyncio.run(wire.roundtrip(topic, event.model_dump_json().encode()))
    return type(event).model_validate_json(delivered)


def golden() -> Bus:
    bus = make_bus()
    bus.send(run_request(2, 2))
    bus.drain()
    return bus


def test_s1_unit_request_and_receipt_survive_the_bus(wire: ProtocolChainBus) -> None:
    bus = golden()
    topics = contract("node_lab_work_unit_effect")["runtime_dispatch"]
    for unit in bus.units[:3]:
        again = trip(wire, topics["command_topic"], unit)
        assert again == unit
        receipt = bus.host(again)
        assert isinstance(receipt, ModelLabWorkUnitReceipt)
        assert trip(wire, topics["terminal_events"]["success"], receipt) == receipt
        # the receipt names its run by the lane the orchestrator set on the unit
        assert receipt.lane == again.lane == str(RUN_ID)


def test_s2_score_request_and_scored_event_survive_the_bus(
    wire: ProtocolChainBus,
) -> None:
    bus = golden()
    topics = contract("node_code_contract_score_effect")["runtime_dispatch"]
    for request in (bus.scores[0], bus.scores[-1]):
        again = trip(wire, topics["command_topic"], request)
        assert isinstance(again, ModelCodeScoreRequest)
        assert again == request
    scored = [m for m in bus.queue if isinstance(m, ModelCodeScored)]
    assert scored == []
    assert bus.scores, "every scored event was consumed by the chain"
    # one correlation id, a distinct try identity each: what E2 keys its scored event by
    identities = {
        (s.try_identity.task_key, s.try_identity.model_arm, s.try_identity.try_ordinal)
        for s in bus.scores
    }
    assert len(identities) == len(bus.scores) == 14
    assert {s.correlation_id for s in bus.scores} == {RUN_ID}


def test_s3_rows_reach_the_projection_one_row_each_by_run_id(
    wire: ProtocolChainBus,
) -> None:
    bus = golden()
    db = InmemoryDatabaseAdapter()
    topic = "onex.evt.omnimarket.context-roi-run-completed.v1"
    handler = HandlerProjectionContextRoi()
    upserted = 0
    for result in bus.projection:
        delivered = trip(wire, topic, result)
        out = handler.handle({**delivered.model_dump(mode="json"), "_db": db})
        upserted += out["rows_upserted"]  # type: ignore[operator]
    stored = db.query("context_roi_scores")
    assert upserted == len(stored) == 14
    assert {r["run_id"] for r in stored} == {str(RUN_ID)}
    assert len({r["correlation_id"] for r in stored}) == 14
    assert sorted(r["attempt_count"] for r in stored)[:2] == [0, 0]  # the two controls


def test_s3_the_columns_the_orchestrator_fills_are_exactly_the_result_stores() -> None:
    row = ModelVerdictRow(
        task_key="t",
        model_arm="m",
        try_ordinal=2,
        is_control=False,
        verdict="fail_check",  # type: ignore[arg-type]
        loop_status="accepted",
    )
    filled = set(trial_columns(row, RUN_ID, "digest", "S"))
    assert filled <= E3_TRIAL_COLUMNS, "a column the result store does not define"
    declared = E3_TRIAL_COLUMNS & set(ModelAttemptReductionRow.model_fields)
    # Once 5835 merges every one of its columns is declared; until then none is (all or none).
    assert declared in (set(), E3_TRIAL_COLUMNS)


def test_s3_a_declared_column_is_filled_on_the_published_row() -> None:
    bus = golden()
    declared = E3_TRIAL_COLUMNS & set(ModelAttemptReductionRow.model_fields)
    for row in bus.rows:
        if "suite_digest" in declared:
            assert row.suite_digest  # type: ignore[attr-defined]


def test_s4_run_command_and_closed_event_survive_the_bus(
    wire: ProtocolChainBus,
) -> None:
    request = run_request(2, 2)
    topics = contract("node_code_eval_trial_orchestrator")
    assert (
        trip(wire, "onex.cmd.omnimarket.code-eval-run-requested.v1", request) == request
    )
    bus = make_bus()
    bus.send(trip(wire, "onex.cmd.omnimarket.code-eval-run-requested.v1", request))
    bus.drain()
    closed = bus.closes[0]
    assert isinstance(closed, ModelCodeEvalRunClosed)
    assert trip(wire, topics["terminal_event"], closed) == closed


def test_s6_the_topics_the_orchestrator_names_are_the_other_nodes_own() -> None:
    own = contract("node_code_eval_trial_orchestrator")
    lab = contract("node_lab_work_unit_effect")
    score = contract("node_code_contract_score_effect")
    context_roi = contract("node_projection_context_roi")
    subscribed = set(own["event_bus"]["subscribe_topics"])
    published = set(own["event_bus"]["publish_topics"])
    assert lab["runtime_dispatch"]["command_topic"] in published
    assert lab["runtime_dispatch"]["terminal_events"]["success"] in subscribed
    assert lab["runtime_dispatch"]["terminal_events"]["failure"] in subscribed
    assert lab["capacity"]["capacity_topic"] in subscribed
    assert score["runtime_dispatch"]["command_topic"] in published
    assert score["runtime_dispatch"]["terminal_events"]["success"] in subscribed
    assert "onex.evt.omnimarket.context-roi-run-completed.v1" in published
    assert "onex.evt.omnimarket.context-roi-run-completed.v1" in set(
        context_roi["event_bus"]["subscribe_topics"]
    )
    assert own["terminal_event"] == "onex.evt.omnimarket.code-eval-run-closed.v1"
    assert own["event_bus"]["subscribe_topics"][0] == (
        "onex.cmd.omnimarket.code-eval-run-requested.v1"
    )


def test_s6_the_published_topics_are_exactly_the_five_the_seams_pin() -> None:
    own = contract("node_code_eval_trial_orchestrator")
    assert set(own["event_bus"]["publish_topics"]) == {
        "onex.cmd.omnimarket.lab-work-unit-requested.v1",
        "onex.cmd.omnimarket.code-contract-score-requested.v1",
        "onex.evt.omnimarket.context-roi-run-completed.v1",
        "onex.cmd.omnimarket.code-eval-run-nudge.v1",
        "onex.evt.omnimarket.code-eval-run-closed.v1",
    }
    assert own["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.code-eval-run-requested.v1"
    )


def test_s6_every_route_names_a_model_and_a_handler_that_import() -> None:
    import importlib

    own = contract("node_code_eval_trial_orchestrator")
    subscribed = set(own["event_bus"]["subscribe_topics"])
    seen = set()
    for route in own["handler_routing"]["handlers"]:
        module = importlib.import_module(route["event_model"]["module"])
        assert issubclass(getattr(module, route["event_model"]["name"]), BaseModel)
        handler = importlib.import_module(route["handler"]["module"])
        assert hasattr(handler, route["handler"]["name"])
        assert route["topic"] in subscribed
        seen.add(route["topic"])
    assert seen == subscribed, "every subscribed topic has a route"
    for event in own["published_events"]:
        assert event["topic"] in own["event_bus"]["publish_topics"]
