# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the three-part proof of the seam graph, each planted fault refused and then restored."""

from __future__ import annotations

import copy
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)
from omnibase_core.validation.validator_event_chain_walker import (
    ValidatorEventChainWalker,
)

from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    EnumSeamSide,
    EnumSeamUnit,
    ModelSeamDeclaration,
)
from omnimarket.models.seam_reading import (
    EnumSeamDisposition as D,
)
from omnimarket.nodes.node_seam_conservation_compute.handlers.handler_seam_conservation import (
    HandlerSeamConservation,
)
from omnimarket.nodes.node_seam_conservation_compute.handlers.handler_seam_graph_check import (
    HandlerSeamGraphCheck,
)
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_conservation_request import (
    ModelSeamConservationRequest,
)
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_graph_check import (
    ModelSeamGraphCheckRequest,
)
from omnimarket.nodes.node_seam_conservation_compute.tests.seam_fixtures import (
    CLOSE_TOPIC,
    COUNT_TOPIC,
    NODES,
    ORCHESTRATOR_CONTRACT,
    READING_TOPIC,
    T0,
    WINDOW,
    close,
    count,
    ident,
    load_graph,
    published_topics,
    seam_nodes,
)

pytestmark = pytest.mark.unit

SEAM_NODES = (
    "node_seam_conservation_compute",
    "node_view_seam_reading_reducer",
    "node_view_render_web_effect",
    "node_view_render_text_effect",
    "node_view_render_slack_effect",
    "node_view_app_interface_effect",
    "node_view_receipt_endpoint_effect",
    "node_view_status_line_receipt_effect",
    "node_factory_floor_effect",
    "node_factory_floor_publish_effect",
    "node_slack_publish_effect",
)
EXISTING_NODES = {
    "node_factory_floor_effect",
    "node_factory_floor_publish_effect",
    "node_slack_publish_effect",
}


# ---- part 1: core's ValidatorEventChainWalker ----------------------------------------


def _walker() -> ValidatorEventChainWalker:
    text = (
        resources.files("omnibase_core.validation.contracts")
        / "event_chain_walker.validation.yaml"
    ).read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    return ValidatorEventChainWalker(
        contract=ModelValidatorSubcontract.model_validate(document["validation"])
    )


def _new_contract_issues(paths: list[Path]) -> list[str]:
    result = _walker().validate(
        [p for p in paths if p.parent.name not in EXISTING_NODES]
    )
    return [i.message for i in result.issues]


def _copy_corpus(tmp_path: Path) -> dict[str, Path]:
    copies: dict[str, Path] = {}
    for name in SEAM_NODES:
        target = tmp_path / name / "contract.yaml"
        target.parent.mkdir()
        shutil.copyfile(NODES / name / "contract.yaml", target)
        copies[name] = target
    return copies


def test_three_part_proof_walker_shows_no_unconsumed_and_no_unproduced_topic(
    tmp_path: Path,
) -> None:
    paths = list(_copy_corpus(tmp_path).values())
    assert _new_contract_issues(paths) == []


def test_three_part_proof_walker_refuses_a_planted_unconsumed_topic_then_restores(
    tmp_path: Path,
) -> None:
    corpus = _copy_corpus(tmp_path)
    paths = list(corpus.values())
    reducer = corpus["node_view_seam_reading_reducer"]
    original = reducer.read_text(encoding="utf-8")
    document: dict[str, Any] = yaml.safe_load(original)
    document["event_bus"]["subscribe_topics"] = []
    document["handler_routing"]["handlers"] = []
    reducer.write_text(yaml.safe_dump(document), encoding="utf-8")
    planted = _new_contract_issues(paths)
    assert planted, "removing the only consumer of a seam topic must be refused"
    reducer.write_text(original, encoding="utf-8")
    assert _new_contract_issues(paths) == []


def test_three_part_proof_walker_refuses_a_planted_unproduced_topic_then_restores(
    tmp_path: Path,
) -> None:
    corpus = _copy_corpus(tmp_path)
    paths = list(corpus.values())
    conservation = corpus["node_seam_conservation_compute"]
    original = conservation.read_text(encoding="utf-8")
    document: dict[str, Any] = yaml.safe_load(original)
    document["event_bus"]["publish_topics"] = []
    document["published_events"] = []
    conservation.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert _new_contract_issues(paths), (
        "a consumed seam topic with no producer must be refused"
    )
    conservation.write_text(original, encoding="utf-8")
    assert _new_contract_issues(paths) == []


# ---- part 2: the graph check ---------------------------------------------------------


def graph_check(graph: dict[str, Any], nodes_dir: Path = NODES) -> list[str]:
    """Every seam side names a node whose contract publishes the count and close topics."""
    declarations = tuple(
        ModelSeamDeclaration.from_graph_node(n) for n in seam_nodes(graph)
    )
    emitters = {e for d in declarations for e in (d.sender_emitter, d.receiver_emitter)}
    topics = {}
    for emitter in emitters:
        path = nodes_dir / emitter / "contract.yaml"
        if path.is_file():
            topics[emitter] = tuple(
                sorted(published_topics(yaml.safe_load(path.read_text("utf-8"))))
            )
    return list(
        HandlerSeamGraphCheck()
        .handle(
            ModelSeamGraphCheckRequest(
                declarations=declarations,
                expected_seam_ids=tuple(f"VS-{n:02d}" for n in range(1, 18)),
                emitter_topics=topics,
                required_topics=(COUNT_TOPIC, CLOSE_TOPIC),
            )
        )
        .faults
    )


def test_three_part_proof_graph_check_passes_for_the_declared_graph() -> None:
    assert graph_check(load_graph()) == []


def test_three_part_proof_graph_check_refuses_a_graph_with_one_emitter_removed_then_restores() -> (
    None
):
    graph = load_graph()
    planted = copy.deepcopy(graph)
    victim = seam_nodes(planted)[4]
    removed = victim["step_config"]["emitters"].pop("receiver")
    assert removed
    faults = graph_check(planted)
    assert len(faults) == 1
    assert "no emitter" in faults[0]
    victim["step_config"]["emitters"]["receiver"] = removed
    assert graph_check(planted) == []


def test_three_part_proof_graph_check_refuses_an_entire_missing_seam_then_restores() -> (
    None
):
    graph = load_graph()
    assert graph_check(graph) == []
    planted = copy.deepcopy(graph)
    nodes = planted["execution_graph"]["nodes"]
    victim = seam_nodes(planted)[-1]
    nodes.remove(victim)
    assert graph_check(planted) == ["VS-17: no seam declaration"]
    nodes.append(victim)
    assert graph_check(planted) == []


def test_three_part_proof_graph_check_refuses_duplicate_seam_then_restores() -> None:
    graph = load_graph()
    assert graph_check(graph) == []
    planted = copy.deepcopy(graph)
    nodes = planted["execution_graph"]["nodes"]
    nodes.append(copy.deepcopy(seam_nodes(planted)[0]))
    assert graph_check(planted) == ["VS-01: duplicate seam declaration"]
    nodes.pop()
    assert graph_check(planted) == []


def test_three_part_proof_graph_check_refuses_an_emitter_whose_contract_lacks_the_topic(
    tmp_path: Path,
) -> None:
    corpus = _copy_corpus(tmp_path)
    graph = load_graph()
    assert graph_check(graph, tmp_path) == []
    web = corpus["node_view_render_web_effect"]
    original = web.read_text(encoding="utf-8")
    document: dict[str, Any] = yaml.safe_load(original)
    document["published_events"] = []
    web.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert graph_check(graph, tmp_path)
    web.write_text(original, encoding="utf-8")
    assert graph_check(graph, tmp_path) == []


def test_three_part_proof_orchestrator_contract_is_what_the_graph_check_reads() -> None:
    assert ORCHESTRATOR_CONTRACT.is_file()
    assert READING_TOPIC.endswith("view-seam-reading-recorded.v1")


# ---- part 3: the runtime check -------------------------------------------------------


def _request(
    *,
    sender_close: bool,
    receiver_close: bool,
    sender_crossings: tuple[str, ...],
) -> ModelSeamConservationRequest:
    declaration = ModelSeamDeclaration.from_graph_node(
        next(
            n
            for n in seam_nodes(load_graph())
            if n["step_config"]["seam_id"] == "VS-02"
        )
    )
    sender = tuple(
        count(
            "VS-02",
            EnumSeamSide.SENDER,
            cid,
            {
                "lines": (EnumSeamUnit.LINES, [ident("line-a", D.HANDED_OVER)]),
                "sections": (EnumSeamUnit.SECTIONS, []),
            },
        )
        for cid in sender_crossings
    )
    receiver = (
        (
            count(
                "VS-02",
                EnumSeamSide.RECEIVER,
                "c-1",
                {
                    "lines": (EnumSeamUnit.LINES, [ident("line-a", D.ACCEPTED)]),
                    "sections": (EnumSeamUnit.SECTIONS, []),
                },
            ),
        )
        if sender_crossings
        else ()
    )
    return ModelSeamConservationRequest(
        declaration=declaration,
        window=WINDOW,
        tick_id="tick-1",
        sender_counts=sender,
        receiver_counts=receiver,
        sender_close=close("VS-02", EnumSeamSide.SENDER, sender_crossings)
        if sender_close
        else None,
        receiver_close=close(
            "VS-02", EnumSeamSide.RECEIVER, ("c-1",) if sender_crossings else ()
        )
        if receiver_close
        else None,
        latency_budget_seconds=60.0,
        read_at=T0,
    )


def _state(request: ModelSeamConservationRequest) -> EnumSeamCrossingState:
    return HandlerSeamConservation().handle(request).state


def test_three_part_proof_runtime_missing_sender_close_reads_unreadable_then_restores() -> (
    None
):
    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    assert _state(clean) is EnumSeamCrossingState.MATCHED
    planted = clean.model_copy(update={"sender_close": None})
    assert _state(planted) is EnumSeamCrossingState.UNREADABLE
    assert _state(clean) is EnumSeamCrossingState.MATCHED


def test_three_part_proof_runtime_missing_receiver_close_with_crossings_reads_unreadable_then_restores() -> (
    None
):
    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    planted = clean.model_copy(update={"receiver_close": None})
    result = HandlerSeamConservation().handle(planted)
    assert result.state is EnumSeamCrossingState.UNREADABLE
    assert result.reading.unreadable == 1
    assert _state(clean) is EnumSeamCrossingState.MATCHED


def test_three_part_proof_runtime_empty_sender_close_and_no_receiver_close_reads_matched() -> (
    None
):
    idle = _request(sender_close=True, receiver_close=False, sender_crossings=())
    result = HandlerSeamConservation().handle(idle)
    assert result.state is EnumSeamCrossingState.MATCHED
    assert result.reading.crossings == 1
    assert result.reading.unreadable == 0


def test_three_part_proof_runtime_both_missing_reads_unreadable_not_matched() -> None:
    nothing = _request(sender_close=False, receiver_close=False, sender_crossings=())
    assert _state(nothing) is EnumSeamCrossingState.UNREADABLE


@pytest.mark.parametrize("side", ["sender", "receiver"])
@pytest.mark.parametrize("fault", ["seam", "side", "window", "watermark"])
def test_three_part_proof_runtime_close_must_cover_this_seam_side_and_window(
    side: str, fault: str
) -> None:
    from datetime import timedelta

    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    held = getattr(clean, f"{side}_close")
    assert held is not None
    updates: dict[str, Any] = {
        "seam": {"seam_id": "VS-01"},
        "side": {
            "side": EnumSeamSide.RECEIVER if side == "sender" else EnumSeamSide.SENDER
        },
        "window": {
            "window": WINDOW.model_copy(
                update={"end": WINDOW.end + timedelta(minutes=1)}
            )
        },
        "watermark": {"reported_through": WINDOW.end - timedelta(seconds=1)},
    }[fault]
    planted = clean.model_copy(
        update={f"{side}_close": held.model_copy(update=updates)}
    )
    assert _state(planted) is EnumSeamCrossingState.UNREADABLE
    assert _state(clean) is EnumSeamCrossingState.MATCHED


@pytest.mark.parametrize(
    ("sender_close", "receiver_close", "crossings", "expected"),
    [
        (True, True, ("c-1",), "matched"),
        (False, True, ("c-1",), "unreadable"),
        (True, False, ("c-1",), "unreadable"),
        (True, False, (), "matched"),
    ],
)
async def test_three_part_proof_node_roundtrip_profile(
    sender_close: bool, receiver_close: bool, crossings: tuple[str, ...], expected: str
) -> None:
    """One fixture, through the contract-routed node on either injected bus profile.

    The Redpanda leg requires an operator-supplied overlay containing ``kafka``
    settings and fails when it is absent. It never substitutes an in-memory bus.
    """
    import os
    from uuid import uuid4

    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
        run_local,
    )
    from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
        ModelRunLocalConfig,
    )

    contract = yaml.safe_load(
        (NODES / "node_seam_conservation_compute" / "contract.yaml").read_text()
    )
    proof = contract["config"]["proof"]
    profile = os.environ.get(proof["profile_env"], proof["default_profile"])
    transport = proof["profiles"][profile]
    bus = None
    if transport == "kafka":
        overlay = yaml.safe_load(Path(os.environ[proof["overlay_env"]]).read_text())
        bus = EventBusKafka(
            config=ModelKafkaEventBusConfig.model_validate(overlay["kafka"])
        )
    else:
        assert transport == "inmemory"
    config = ModelRunLocalConfig.model_validate(proof["run_local"])
    config = config.model_copy(
        update={"consumer_group": f"{config.consumer_group}-{uuid4().hex}"}
    )
    request = _request(
        sender_close=sender_close,
        receiver_close=receiver_close,
        sender_crossings=crossings,
    )
    result = await run_local(
        "node_seam_conservation_compute",
        "reconcile_seam",
        request.model_dump(mode="json"),
        config,
        bus=bus,
    )
    assert result["state"] == expected


@pytest.mark.parametrize(
    "fault", ["unit", "kind", "duplicate-id", "side", "negative-latency"]
)
def test_three_part_proof_runtime_refuses_malformed_measure_then_restores(
    fault: str,
) -> None:
    from omnimarket.models.seam_reading import EnumSeamMeasureKind

    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    row = clean.receiver_counts[0]
    measure = row.measures[0]
    if fault == "side":
        row = row.model_copy(update={"side": EnumSeamSide.SENDER})
    else:
        updates: dict[str, Any] = {
            "unit": {"unit": EnumSeamUnit.SOURCES},
            "kind": {"kind": EnumSeamMeasureKind.COVERAGE},
            "duplicate-id": {"ids": (*measure.ids, measure.ids[0])},
            "negative-latency": {
                "ids": (measure.ids[0].model_copy(update={"stamp": T0}),)
            },
        }[fault]
        row = row.model_copy(
            update={"measures": (measure.model_copy(update=updates), *row.measures[1:])}
        )
    planted = clean.model_copy(update={"receiver_counts": (row,)})
    assert _state(planted) is EnumSeamCrossingState.MISMATCHED
    assert _state(clean) is EnumSeamCrossingState.MATCHED


def test_three_part_proof_runtime_does_not_cancel_residuals_between_crossings() -> None:
    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    sender = tuple(
        count(
            "VS-02",
            EnumSeamSide.SENDER,
            cid,
            {
                "lines": (EnumSeamUnit.LINES, [ident(line, D.HANDED_OVER)]),
                "sections": (EnumSeamUnit.SECTIONS, []),
            },
        )
        for cid, line in (("c-1", "a"), ("c-2", "b"))
    )
    receiver = tuple(
        count(
            "VS-02",
            EnumSeamSide.RECEIVER,
            cid,
            {
                "lines": (EnumSeamUnit.LINES, [ident(line, D.ACCEPTED)]),
                "sections": (EnumSeamUnit.SECTIONS, []),
            },
        )
        for cid, line in (("c-1", "a"), ("c-2", "b"))
    )
    paired = clean.model_copy(
        update={
            "sender_counts": sender,
            "receiver_counts": receiver,
            "sender_close": close("VS-02", EnumSeamSide.SENDER, ("c-1", "c-2")),
            "receiver_close": close("VS-02", EnumSeamSide.RECEIVER, ("c-1", "c-2")),
        }
    )
    assert _state(paired) is EnumSeamCrossingState.MATCHED
    swapped = tuple(
        row.model_copy(
            update={"crossing_id": "c-2" if row.crossing_id == "c-1" else "c-1"}
        )
        for row in receiver
    )
    assert (
        _state(paired.model_copy(update={"receiver_counts": swapped}))
        is EnumSeamCrossingState.MISMATCHED
    )
    assert _state(paired) is EnumSeamCrossingState.MATCHED


def test_three_part_proof_runtime_refuses_a_different_counterpart_crossing_id() -> None:
    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    assert _state(clean) is EnumSeamCrossingState.MATCHED
    receiver = clean.receiver_counts[0].model_copy(update={"crossing_id": "other"})
    planted = clean.model_copy(
        update={
            "receiver_counts": (receiver,),
            "receiver_close": close("VS-02", EnumSeamSide.RECEIVER, ("other",)),
        }
    )
    assert _state(planted) is EnumSeamCrossingState.MISMATCHED
    assert _state(clean) is EnumSeamCrossingState.MATCHED


@pytest.mark.parametrize("fault", ["missing", "duplicate"])
def test_three_part_proof_runtime_requires_each_declared_measure_once(
    fault: str,
) -> None:
    clean = _request(sender_close=True, receiver_close=True, sender_crossings=("c-1",))
    row = clean.receiver_counts[0]
    measures = () if fault == "missing" else (*row.measures, row.measures[0])
    updates: dict[str, Any] = {
        "receiver_counts": (row.model_copy(update={"measures": measures}),)
    }
    if fault == "missing":
        updates["sender_counts"] = (
            clean.sender_counts[0].model_copy(update={"measures": ()}),
        )
    planted = clean.model_copy(update=updates)
    assert _state(planted) is EnumSeamCrossingState.MISMATCHED
    assert _state(clean) is EnumSeamCrossingState.MATCHED
