# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_contract_seams_orchestrator: the graph, the reducer it folds into, and real runs (OMN-21627).

Three proofs the ticket names: the event-chain walk over the orchestrator graph and the
reducer FSM, a planted design that fails at a mismatched seam, and one real run of a
two-node toy design on the in-memory overlay.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.runtime.runtime_local import RuntimeLocal
from omnibase_core.validation.validator_contract_walker import walk_contracts

from omnimarket.models.contract_seams_design import ModelContractSeamsDesign
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_seams_run import (
    HandlerContractSeamsRun,
)
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_steps import (
    ModelDrivenContractChain,
    drive_contract_chain,
)
from omnimarket.nodes.node_contract_seams_orchestrator.models.model_contract_seams_run import (
    ModelContractSeamsRunRequest,
    ModelContractSeamsRunResult,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).with_name("fixtures")
NODES = Path(str(files("omnimarket.nodes")))
ORCHESTRATOR = NODES / "node_contract_seams_orchestrator" / "contract.yaml"
REDUCER = NODES / "node_contract_seams_reducer" / "contract.yaml"
GOLDEN_TRIGGERS = (
    "contracts_emitted",
    "nodes_scaffolded",
    "seams_read",
    "chains_walked",
    "golden_chain_swept",
    "chain_connected",
)


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 31, 4, 0, tzinfo=UTC)


def _design(name: str) -> ModelContractSeamsDesign:
    return ModelContractSeamsDesign.model_validate(
        yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    )


def _run(
    design: ModelContractSeamsDesign, output_root: Path, bus: EventBusInmemory
) -> ModelContractSeamsRunResult:
    return HandlerContractSeamsRun(event_bus=bus, clock=_FixedClock()).handle(
        ModelContractSeamsRunRequest(
            correlation_id=uuid.UUID(int=21627),
            design=design,
            output_root=str(output_root),
        )
    )


def _graph() -> list[dict[str, object]]:
    document = yaml.safe_load(ORCHESTRATOR.read_text(encoding="utf-8"))
    nodes: list[dict[str, object]] = document["workflow_coordination"][
        "workflow_definition"
    ]["execution_graph"]["nodes"]
    return nodes


def _bus() -> EventBusInmemory:
    return EventBusInmemory(environment="test", group="contract-seams-test")


def _history_topics(bus: EventBusInmemory) -> list[str]:
    return [message.topic for message in asyncio.run(bus.get_event_history(limit=1000))]


# -- proof 1: the event-chain walk over the orchestrator graph and the reducer FSM -------


def test_orchestrator_and_reducer_topics_close_over_each_other() -> None:
    """Every topic one of the pair publishes, the other subscribes, or the world does."""
    documents = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in (ORCHESTRATOR, REDUCER)
    ]
    published = {t for d in documents for t in d["event_bus"]["publish_topics"]}
    subscribed = {t for d in documents for t in d["event_bus"]["subscribe_topics"]}
    external_out = {
        t for d in documents for t in d.get("externally_consumed_topics", [])
    }
    external_in = {d["runtime_dispatch"]["command_topic"] for d in documents}
    assert published - subscribed - external_out == set()
    assert subscribed - published - external_in == set()


def test_reducer_fsm_walks_one_golden_path_in_graph_order() -> None:
    """The walker reads the reducer's machine; the graph's triggers are its golden path.

    The core walker does not read an execution graph yet (plan task T-C7), so the graph is
    tied to the machine here: the triggers the graph folds, in graph order, are exactly the
    walker's golden path, and every trigger a step names is a declared transition.
    """
    report = walk_contracts([ORCHESTRATOR.parent, REDUCER.parent])
    assert report.summary.workflows == 1
    assert report.summary.golden_paths == 1
    assert report.summary.dead_states == 0
    assert report.summary.unreachable_states == 0
    assert report.summary.uncovered_transitions == 0
    golden = next(
        path
        for workflow in report.workflows
        for path in workflow.paths
        if path.kind.value == "golden"
    )
    assert tuple(step.trigger for step in golden.steps) == GOLDEN_TRIGGERS
    graph_triggers = tuple(
        config["trigger"]
        for node in _graph()
        if isinstance(config := node.get("step_config"), dict) and "trigger" in config
    )
    assert graph_triggers == GOLDEN_TRIGGERS


def test_every_graph_step_names_a_node_that_routes_its_operation() -> None:
    for node in _graph():
        config = node["step_config"]
        assert isinstance(config, dict)
        if "node" not in config:
            continue
        contract = yaml.safe_load(
            (NODES / str(config["node"]) / "contract.yaml").read_text(encoding="utf-8")
        )
        operations = {
            entry["operation"] for entry in contract["handler_routing"]["handlers"]
        }
        assert config["node_operation"] in operations, node["node_id"]


# -- proof 3: one real run of the two-node toy design on the in-memory overlay ----------


def test_toy_design_connects_on_the_in_memory_overlay(tmp_path: Path) -> None:
    bus = _bus()
    result = _run(_design("toy_design.yaml"), tmp_path, bus)

    assert result.final_state == "connected", result.failure
    assert result.refused_by is None
    assert result.failure is None
    assert result.steps == tuple(str(node["node_id"]) for node in _graph())
    assert tuple(t.trigger for t in result.transitions) == GOLDEN_TRIGGERS
    assert all(t.moved for t in result.transitions)
    assert result.scaffolded_nodes == (
        "node_toy_intake_compute",
        "node_toy_record_effect",
    )
    assert [(v.crossing_id, v.verdict) for v in result.seam_verdicts] == [
        ("toy.intake_accepted", "MATCHED")
    ]
    assert result.walk_issues == ()
    assert result.chain_event_types == (
        "onex.cmd.toy.intake-requested.v1",
        "onex.evt.toy.intake-accepted.v1",
        "onex.evt.toy.recorded.v1",
    )
    assert result.chain_states == ("node_toy_intake_compute", "node_toy_record_effect")
    assert result.undriven_path_ids == ()
    assert result.sweep_status == "pass"

    # Each scaffold carries the contract the run wrote, and its handler module exists.
    for name in result.scaffolded_nodes:
        contract = yaml.safe_load(
            (tmp_path / name / "contract.yaml").read_text(encoding="utf-8")
        )
        module = contract["handler"]["module"].rpartition(".")[2]
        assert (tmp_path / name / "handlers" / f"{module}.py").is_file()
        assert contract["seams"][0]["id"] == "toy.intake_accepted"

    # The run's own chain is on the bus: every reducer fold and every node it ran.
    topics = _history_topics(bus)
    assert (
        topics.count("onex.cmd.omnimarket.contract-seams-transition-requested.v1") == 6
    )
    assert topics.count("onex.evt.omnimarket.contract-seams-transitioned.v1") == 6
    for topic in (
        "onex.cmd.omnimarket.generate-node-start.v1",
        "onex.cmd.omnimarket.seam-graph-requested.v1",
        "onex.evt.omnimarket.seam-match-completed.v1",
        "onex.evt.omnimarket.event-chain-generator-completed.v1",
    ):
        assert topic in topics


def test_core_local_runtime_runs_the_toy_design_from_the_contract(
    tmp_path: Path,
) -> None:
    """The in-memory overlay as the runtime binds it: core's RuntimeLocal reads the
    orchestrator's contract, injects its in-memory bus, publishes the run command and
    waits for the contract's terminal event."""
    contract = yaml.safe_load(ORCHESTRATOR.read_text(encoding="utf-8"))
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.contract-seams-run-completed.v1"
    )
    request = tmp_path / "request.json"
    request.write_text(
        ModelContractSeamsRunRequest(
            correlation_id=uuid.UUID(int=21628),
            design=_design("toy_design.yaml"),
            output_root=str(tmp_path / "out"),
        ).model_dump_json(),
        encoding="utf-8",
    )
    runtime = RuntimeLocal(
        ORCHESTRATOR, state_root=tmp_path / "state", input_path=request, timeout=60
    )

    assert runtime.run().value == "completed"
    record = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    terminal = ModelContractSeamsRunResult.model_validate(record["terminal_payload"])
    assert terminal.final_state == "connected"
    assert terminal.chain_event_types[-1] == "onex.evt.toy.recorded.v1"


def test_a_dropped_subscription_breaks_the_drive(tmp_path: Path) -> None:
    """The drive is not vacuous: drop one subscription from a scaffolded contract."""
    assert _run(_design("toy_design.yaml"), tmp_path, _bus()).final_state == "connected"
    documents = {
        path.parent.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(tmp_path.glob("*/contract.yaml"))
    }
    documents["node_toy_record_effect"]["event_bus"]["subscribe_topics"] = []

    async def drive() -> ModelDrivenContractChain:
        bus = _bus()
        await bus.start()
        try:
            return await drive_contract_chain(
                bus,
                documents,
                head_topic="onex.cmd.toy.intake-requested.v1",
                tail_topic="onex.evt.toy.recorded.v1",
                correlation_id="planted",
                group_prefix="planted",
            )
        finally:
            await bus.close()

    chain = asyncio.run(drive())
    assert chain.delivered is False
    assert chain.event_types == ("onex.cmd.toy.intake-requested.v1",)


# -- proof 2: a planted design that fails at a mismatched seam --------------------------


def test_planted_mismatched_seam_refuses_at_match_seams(tmp_path: Path) -> None:
    result = _run(_design("toy_design_mismatched_seam.yaml"), tmp_path, _bus())

    assert result.final_state == "refused"
    assert result.refused_by == "match_seams"
    assert result.steps == (
        "emit_contracts",
        "scaffold_nodes",
        "read_seam_graph",
        "match_seams",
    )
    assert [
        (v.crossing_id, v.verdict, v.mismatching_field_path)
        for v in result.seam_verdicts
    ] == [("toy.intake_accepted", "MISMATCH", "envelope_model")]
    assert result.transitions[-1].trigger == "seams_read"
    assert result.transitions[-1].state == "refused"
    # Nothing after the seam ran: no walk, no drive, no sweep.
    assert result.chain_event_types == ()
    assert result.sweep_status is None


def test_design_with_an_unconnected_node_refuses_before_any_contract(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load((FIXTURES / "toy_design.yaml").read_text(encoding="utf-8"))
    raw["nodes"].append(
        {
            "node_name": "node_toy_orphan_compute",
            "archetype": "compute",
            "description": "A node no crossing reaches.",
        }
    )
    result = _run(ModelContractSeamsDesign.model_validate(raw), tmp_path, _bus())

    assert result.final_state == "refused"
    assert result.refused_by == "emit_contracts"
    assert result.design_issues == ("node node_toy_orphan_compute has no crossing",)
    assert list(tmp_path.iterdir()) == []


def test_a_step_that_raises_fails_the_run_and_names_it(tmp_path: Path) -> None:
    occupied = tmp_path / "node_toy_intake_compute"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("not empty", encoding="utf-8")

    result = _run(_design("toy_design.yaml"), tmp_path, _bus())

    assert result.final_state == "failed"
    assert result.failure is not None
    assert result.failure.startswith("scaffold_nodes: FileExistsError")
    assert result.transitions[-1].trigger == "step_failed"
