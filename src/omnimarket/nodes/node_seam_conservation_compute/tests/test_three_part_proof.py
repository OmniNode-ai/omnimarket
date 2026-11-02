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
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_conservation_request import (
    ModelSeamConservationRequest,
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
FAMILY = ("view-seam-counted", "view-seam-window-closed", "view-seam-reading-recorded")


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


def _family_issues(paths: list[Path]) -> list[str]:
    result = _walker().validate(paths)
    return [i.message for i in result.issues if any(t in i.message for t in FAMILY)]


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
    assert _family_issues(paths) == []


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
    planted = _family_issues(paths)
    assert planted, "removing the only consumer of a seam topic must be refused"
    reducer.write_text(original, encoding="utf-8")
    assert _family_issues(paths) == []


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
    assert _family_issues(paths), (
        "a consumed seam topic with no producer must be refused"
    )
    conservation.write_text(original, encoding="utf-8")
    assert _family_issues(paths) == []


# ---- part 2: the graph check ---------------------------------------------------------


def graph_check(graph: dict[str, Any], nodes_dir: Path = NODES) -> list[str]:
    """Every seam side names a node whose contract publishes the count and close topics."""
    faults: list[str] = []
    for node in seam_nodes(graph):
        declaration = ModelSeamDeclaration.from_graph_node(node)
        for side, emitter in (
            ("sender", declaration.sender_emitter),
            ("receiver", declaration.receiver_emitter),
        ):
            if not emitter:
                faults.append(f"{declaration.seam_id} {side}: no emitter")
                continue
            path = nodes_dir / emitter / "contract.yaml"
            if not path.is_file():
                faults.append(
                    f"{declaration.seam_id} {side}: {emitter} has no contract"
                )
                continue
            published = published_topics(yaml.safe_load(path.read_text("utf-8")))
            for topic in (COUNT_TOPIC, CLOSE_TOPIC):
                if topic not in published:
                    faults.append(
                        f"{declaration.seam_id} {side}: {emitter} does not publish {topic}"
                    )
    return faults


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
            {"lines": (EnumSeamUnit.LINES, [ident("line-a", D.HANDED_OVER)])},
        )
        for cid in sender_crossings
    )
    receiver = (
        (
            count(
                "VS-02",
                EnumSeamSide.RECEIVER,
                "r-1",
                {"lines": (EnumSeamUnit.LINES, [ident("line-a", D.ACCEPTED)])},
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
            "VS-02", EnumSeamSide.RECEIVER, ("r-1",) if sender_crossings else ()
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
