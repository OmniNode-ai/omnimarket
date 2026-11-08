# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23136: the corrected cross-dependency baseline is frozen with a hash and the gate goes red on a rise.

The fixture is a synthetic graph of 11 labels in one component whose cut is the plan's five relations
at their frozen counts (a chain of forward imports plus the five back relations). The gate reads the
committed baseline file; the injected breaks (a planted back edge, a tampered baseline) turn it red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.import_relations import (
    EnumDependencyRiseKind,
    ModelDependencyBaseline,
    ModelDependencyGateRequest,
    ModelDependencyGateResult,
    ModelImportEdge,
    ModelImportRelationOverlay,
    ModelImportRelationReport,
    ModelImportRelationRequest,
    ModelRelationMapReadingCommand,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_dependency_gate_compute import (
    HandlerDependencyGateCompute,
    answer_gate_command,
    baseline_from_text,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_relation_map_reading import (
    HandlerRelationMapReading,
    answer_relation_map_command,
    build_relation_map_row,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingPublish

pytestmark = pytest.mark.unit

BASELINE_FILE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_network_graph_compute/frozen_dependency_counts.yaml"
)
NOW = datetime(2026, 11, 8, 13, 0, tzinfo=UTC)
PUBLISH = ModelReadingPublish(
    lane="reading-writer",
    actor="node:node_reading_writer_effect",
    publish_max_age_seconds=7200,
    reading_term="src=reading",
)
TOPICS = load_topics()
ORDER = (
    "ledger-session",
    "f1",
    "tickets-plans",
    "f2",
    "seams-verify",
    "f3",
    "landing",
    "f4",
    "dispatch",
    "f5",
    "intelligence-memory",
)
BACK = (
    ("dispatch", "ledger-session", 177),
    ("dispatch", "tickets-plans", 19),
    ("landing", "tickets-plans", 19),
    ("intelligence-memory", "seams-verify", 12),
    ("intelligence-memory", "landing", 2),
)
EMPTY_OVERLAY = ModelImportRelationOverlay(
    label_rules=(), tracked_relations=(), label_owners=(), joins=()
)


def _edges(source: str, target: str, occurrences: int) -> list[ModelImportEdge]:
    return [
        ModelImportEdge(
            importer_repo=source,
            importer_path=f"s{i}.py",
            imported_repo=target,
            imported_path=f"t{i}.py",
        )
        for i in range(occurrences)
    ]


def _report(
    *, planted: bool = False, extra_on_cut: int = 0
) -> ModelImportRelationReport:
    edges: list[ModelImportEdge] = []
    for lower, higher in pairwise(ORDER):
        edges += _edges(lower, higher, 1)
    for source, target, occurrences in BACK:
        edges += _edges(source, target, occurrences + extra_on_cut)
    if planted:
        edges += _edges("f5", "f1", 1)
    return HandlerImportRelationCompute().handle(
        ModelImportRelationRequest(
            edges=tuple(edges), overlay=EMPTY_OVERLAY, reference_time=NOW
        )
    )


def _baseline() -> ModelDependencyBaseline:
    return baseline_from_text(BASELINE_FILE.read_text(encoding="utf-8"))


def _gate(
    report: ModelImportRelationReport, baseline: ModelDependencyBaseline | None = None
) -> ModelDependencyGateResult:
    return HandlerDependencyGateCompute().handle(
        ModelDependencyGateRequest(
            correlation_id="gate-1", report=report, baseline=baseline or _baseline()
        )
    )


def test_dependency_baseline_frozen_holds_the_corrected_counts_and_its_hash() -> None:
    baseline = _baseline()
    assert baseline.hash_matches()
    assert baseline.subsystems == 11
    assert {
        (r.key.source, r.key.target): r.occurrences for r in baseline.relations
    } == {(s, t): n for s, t, n in BACK}
    assert baseline.total_occurrences == 229
    assert len(baseline.relations) == 5


def test_dependency_baseline_frozen_is_read_by_the_gate_and_the_fixture_passes() -> (
    None
):
    result = _gate(_report())
    assert result.state == "PASS"
    assert (result.current_count, result.frozen_count) == (229, 229)
    assert (result.current_subsystems, result.frozen_subsystems) == (11, 11)
    assert result.baseline_sha256 == _baseline().sha256
    assert result.baseline_intact is True
    assert result.rises == ()


def test_dependency_baseline_frozen_break_a_changed_number_without_a_new_hash_reads_red() -> (
    None
):
    text = BASELINE_FILE.read_text(encoding="utf-8")
    tampered = baseline_from_text(text.replace("subsystems: 11", "subsystems: 12", 1))
    assert not tampered.hash_matches()
    result = _gate(_report(), tampered)
    assert result.state == "RED"
    assert result.baseline_intact is False
    assert [r.kind for r in result.rises] == [
        EnumDependencyRiseKind.BASELINE_HASH_MISMATCH
    ]


def test_planted_back_edge_raises_the_count_above_the_frozen_value_and_goes_red() -> (
    None
):
    clean = _gate(_report())
    planted = _gate(_report(planted=True))
    assert clean.state == "PASS"
    assert planted.state == "RED"
    assert planted.current_count == planted.frozen_count + 1 == 230
    assert planted.residual_occurrences == 1
    kinds = {r.kind for r in planted.rises}
    assert kinds == {
        EnumDependencyRiseKind.RESIDUAL_BACK_EDGE,
        EnumDependencyRiseKind.COUNT_ROSE,
    }


def test_planted_back_edge_on_a_frozen_relation_names_that_relation() -> None:
    result = _gate(_report(extra_on_cut=1))
    assert result.state == "RED"
    assert result.current_count == 229 + 5
    named = sorted(r.detail for r in result.rises if r.kind.value == "relation-rose")
    assert named == sorted(f"{s} to {t}" for s, t, _ in BACK)


def test_planted_back_edge_a_new_subsystem_in_the_component_goes_red() -> None:
    edges = [
        *_edges("dispatch", "f6", 1),
        *_edges("f6", "ledger-session", 1),
    ]
    bigger = HandlerImportRelationCompute().handle(
        ModelImportRelationRequest(
            edges=tuple(_all_edges() + edges),
            overlay=EMPTY_OVERLAY,
            reference_time=NOW,
        )
    )
    result = _gate(bigger)
    assert result.current_subsystems == 12
    assert result.state == "RED"
    assert EnumDependencyRiseKind.SUBSYSTEMS_ROSE in {r.kind for r in result.rises}


def _all_edges() -> list[ModelImportEdge]:
    edges: list[ModelImportEdge] = []
    for lower, higher in pairwise(ORDER):
        edges += _edges(lower, higher, 1)
    for source, target, occurrences in BACK:
        edges += _edges(source, target, occurrences)
    return edges


def _command(
    gate: ModelDependencyGateResult | None, report: ModelImportRelationReport
) -> bytes:
    body: dict[str, Any] = {
        "correlation_id": "map-1",
        "piece": 1,
        "window_start": NOW.isoformat(),
        "report": json.loads(report.model_dump_json()),
        "gate": None if gate is None else json.loads(gate.model_dump_json()),
        "write": True,
    }
    return json.dumps(body).encode()


def test_reading_row_carries_counts_current_and_frozen_in_the_written_row() -> None:
    report = _report()
    command = ModelRelationMapReadingCommand.model_validate_json(
        _command(_gate(report), report)
    )
    row, verdict = build_relation_map_row(
        command, publish=PUBLISH, published_at=NOW, probed_at=NOW
    )
    cells = row.split(" | ")
    assert "count=229" in cells
    assert "frozen=229" in cells
    assert "gate=PASS" in cells
    assert "subsystems=11" in cells
    assert f"baseline={_baseline().sha256[:16]}" in cells
    assert verdict == "WORKING"


def test_reading_row_carries_counts_above_the_frozen_value_when_a_back_edge_is_planted() -> (
    None
):
    report = _report(planted=True)
    command = ModelRelationMapReadingCommand.model_validate_json(
        _command(_gate(report), report)
    )
    row, _ = build_relation_map_row(
        command, publish=PUBLISH, published_at=NOW, probed_at=NOW
    )
    cells = row.split(" | ")
    assert "count=230" in cells
    assert "frozen=229" in cells
    assert "gate=RED" in cells


def test_reading_row_carries_counts_error_chain_an_unread_gate_is_unread_never_zero() -> (
    None
):
    report = _report()
    command = ModelRelationMapReadingCommand.model_validate_json(_command(None, report))
    row, _ = build_relation_map_row(
        command, publish=PUBLISH, published_at=NOW, probed_at=NOW
    )
    cells = row.split(" | ")
    assert {"count=unread", "frozen=unread", "gate=unread"} <= set(cells)
    assert "count=0" not in cells


def _answer(payload: bytes, ledger: InMemoryLedger) -> list[tuple[str, dict[str, Any]]]:
    writer = HandlerRelationMapReading(
        publish=PUBLISH, appender=ledger, clock=lambda: NOW
    )
    return [
        (topic, json.loads(body))
        for topic, body in answer_relation_map_command(payload, writer, TOPICS)
    ]


def test_reading_row_carries_counts_golden_chain_one_written_event_and_one_row() -> (
    None
):
    report = _report()
    ledger = InMemoryLedger()
    events = _answer(_command(_gate(report), report), ledger)
    assert [topic for topic, _ in events] == [TOPICS["relation_map_written"]]
    assert events[0][1]["verdict"] == "WORKING"
    rows = [line for line in ledger.lines if "name=relation_map" in line]
    assert len(rows) == 1
    assert "count=229 | frozen=229" in rows[0]


def test_reading_row_carries_counts_error_chain_a_refused_append_is_a_failed_event() -> (
    None
):
    report = _report(planted=True)
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = _answer(_command(_gate(report), report), ledger)
    assert [topic for topic, _ in events] == [TOPICS["relation_map_failed"]]
    assert events[0][1]["failure"] == "append-failed:refused"
    assert ledger.lines == []


def test_reading_row_carries_counts_error_chain_a_bad_gate_payload_is_a_failed_event() -> (
    None
):
    ledger = InMemoryLedger()
    events = _answer(b'{"piece": 1, "gate": {"state": "PASS"}}', ledger)
    assert [topic for topic, _ in events] == [TOPICS["relation_map_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")
    assert ledger.lines == []


def test_the_baseline_file_is_plain_yaml_with_the_hash_cell() -> None:
    document = yaml.safe_load(BASELINE_FILE.read_text(encoding="utf-8"))
    assert document["sha256"] == _baseline().body_digest()


# -- the gate operation on the in-memory bus: golden and error chain events ----------------------

GATE_CONTRACT = BASELINE_FILE.parent / "contract.yaml"


def _gate_topics() -> dict[str, str]:
    contract = yaml.safe_load(GATE_CONTRACT.read_text(encoding="utf-8"))
    publish = contract["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.dependency-gate-computed.v1" in publish
    assert "onex.evt.omnimarket.dependency-gate-failed.v1" in publish
    return {
        "computed": "onex.evt.omnimarket.dependency-gate-computed.v1",
        "failed": "onex.evt.omnimarket.dependency-gate-failed.v1",
    }


async def _run_on_bus(payload: bytes) -> list[tuple[str, dict[str, Any]]]:
    topics = _gate_topics()
    command_topic = "onex.cmd.omnimarket.dependency-gate-compute-requested.v1"
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_gate_command(
            message.value, HandlerDependencyGateCompute(), topics
        ):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(command_topic, on_message=on_command, group_id="gate")
        for topic in topics.values():
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(command_topic, key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _request_bytes(
    report: ModelImportRelationReport, baseline: ModelDependencyBaseline
) -> bytes:
    return (
        ModelDependencyGateRequest(
            correlation_id="gate-bus-1", report=report, baseline=baseline
        )
        .model_dump_json()
        .encode()
    )


def test_golden_chain_the_gate_command_gives_one_computed_event_that_reads_pass() -> (
    None
):
    events = asyncio.run(_run_on_bus(_request_bytes(_report(), _baseline())))
    assert [topic for topic, _ in events] == [
        "onex.evt.omnimarket.dependency-gate-computed.v1"
    ]
    assert events[0][1]["state"] == "PASS"
    assert events[0][1]["current_count"] == events[0][1]["frozen_count"] == 229


def test_error_chain_a_planted_back_edge_is_a_computed_event_that_reads_red() -> None:
    events = asyncio.run(
        _run_on_bus(_request_bytes(_report(planted=True), _baseline()))
    )
    assert [topic for topic, _ in events] == [
        "onex.evt.omnimarket.dependency-gate-computed.v1"
    ]
    assert events[0][1]["state"] == "RED"
    assert events[0][1]["current_count"] == 230


def test_error_chain_a_bad_gate_request_is_the_typed_failed_event_never_dropped() -> (
    None
):
    events = asyncio.run(_run_on_bus(b'{"correlation_id": "x"}'))
    assert [topic for topic, _ in events] == [
        "onex.evt.omnimarket.dependency-gate-failed.v1"
    ]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


def test_break_a_gate_that_never_reads_red_turns_the_error_chain_assertion_red() -> (
    None
):
    class Lenient(HandlerDependencyGateCompute):
        def handle(
            self, request: ModelDependencyGateRequest
        ) -> ModelDependencyGateResult:
            result = super().handle(request)
            return result.model_copy(update={"state": "PASS", "rises": ()})

    topics = _gate_topics()
    body = _request_bytes(_report(planted=True), _baseline())
    (topic, payload), *_ = answer_gate_command(body, Lenient(), topics)
    assert topic == topics["computed"]
    assert json.loads(payload)["state"] == "PASS"
    with pytest.raises(AssertionError):
        assert json.loads(payload)["state"] == "RED"
