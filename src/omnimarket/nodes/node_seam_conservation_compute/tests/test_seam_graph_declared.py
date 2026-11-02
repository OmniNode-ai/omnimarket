# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: the whole seam graph view_seam_read is declared, with its models, nodes and scaffolds."""

from __future__ import annotations

import importlib

import pytest
import yaml
from omnibase_core.models.contracts.model_contract_workflow_definition import (
    ModelContractWorkflowDefinition,
)

from omnimarket.models.seam_reading import (
    EnumSeamCause,
    EnumSeamSide,
    EnumSeamUnit,
    ModelSeamCount,
    ModelSeamDeclaration,
    ModelSeamMeasure,
    ModelSeamWindowClose,
)
from omnimarket.nodes.node_seam_conservation_compute.tests.seam_fixtures import (
    COUNT_TOPIC,
    NODES,
    load_graph,
    published_topics,
    seam_nodes,
)

pytestmark = pytest.mark.unit

IDS = [f"VS-{n:02d}" for n in range(1, 18)]
SCAFFOLDS = {
    "node_view_render_web_effect": "HandlerViewRenderWebEffect",
    "node_view_render_text_effect": "HandlerViewRenderTextEffect",
    "node_view_render_slack_effect": "HandlerViewRenderSlackEffect",
    "node_view_app_interface_effect": "HandlerViewAppInterfaceEffect",
    "node_view_receipt_endpoint_effect": "HandlerViewReceiptEndpointEffect",
    "node_view_status_line_receipt_effect": "HandlerViewStatusLineReceiptEffect",
}


def test_seam_graph_declared_all_seventeen_seams_in_order() -> None:
    seams = seam_nodes(load_graph())
    assert [s["step_config"]["seam_id"] for s in seams] == IDS


def test_seam_graph_declared_parses_with_the_core_workflow_model() -> None:
    definition = ModelContractWorkflowDefinition.model_validate(load_graph())
    assert definition.workflow_metadata.workflow_name == "view_seam_read"


def test_seam_graph_declared_the_factory_line_graph_is_untouched() -> None:
    raw = yaml.safe_load(
        (NODES / "node_seam_read_orchestrator" / "contract.yaml").read_text("utf-8")
    )
    name = raw["workflow_coordination"]["workflow_definition"]["workflow_metadata"][
        "workflow_name"
    ]
    assert name == "factory_line_seam_read"


def test_seam_graph_declared_each_entry_names_stations_measures_identity_causes_emitters() -> (
    None
):
    for node in seam_nodes(load_graph()):
        declaration = ModelSeamDeclaration.from_graph_node(node)
        assert declaration.sender_station != declaration.receiver_station
        assert declaration.measures, declaration.seam_id
        assert declaration.identity, declaration.seam_id
        assert declaration.sender_emitter, declaration.seam_id
        assert declaration.receiver_emitter, declaration.seam_id
        assert declaration.latency_budget_key, declaration.seam_id
        assert declaration.settlement_deadline_key, declaration.seam_id
        assert declaration.status in {"declared", "measured"}
        for measure in declaration.measures:
            assert isinstance(measure.unit, EnumSeamUnit)


def test_seam_graph_declared_stations_are_graph_nodes_and_seams_sit_between_them() -> (
    None
):
    graph = load_graph()["execution_graph"]["nodes"]
    stations = {n["node_id"] for n in graph if n["node_type"] == "EFFECT_GENERIC"}
    for node in seam_nodes(load_graph()):
        declaration = ModelSeamDeclaration.from_graph_node(node)
        assert declaration.sender_station in stations
        assert declaration.receiver_station in stations
        assert node["depends_on"] == [declaration.sender_station]


def test_seam_graph_declared_vs02_declares_lines_and_sections_and_forbids_refused() -> (
    None
):
    node = next(
        n for n in seam_nodes(load_graph()) if n["step_config"]["seam_id"] == "VS-02"
    )
    declaration = ModelSeamDeclaration.from_graph_node(node)
    assert [m.measure_id for m in declaration.measures] == ["lines", "sections"]
    assert declaration.permits_refused == ()
    assert EnumSeamCause.NO_MATCHING_PLAN in declaration.permits_blocked
    assert declaration.one_emitter is True


def test_seam_graph_declared_vs13_forbids_digest_mismatch() -> None:
    node = next(
        n for n in seam_nodes(load_graph()) if n["step_config"]["seam_id"] == "VS-13"
    )
    declaration = ModelSeamDeclaration.from_graph_node(node)
    assert EnumSeamCause.DIGEST_MISMATCH in declaration.forbidden_causes
    assert EnumSeamCause.ORPHANED in declaration.forbidden_causes


def test_seam_graph_declared_models_and_enums_exist() -> None:
    assert {s.value for s in EnumSeamSide} == {"sender", "receiver"}
    assert ModelSeamCount.model_config["frozen"] is True
    assert ModelSeamCount.model_config["extra"] == "forbid"
    assert ModelSeamMeasure.model_config["extra"] == "forbid"
    assert ModelSeamWindowClose.model_config["frozen"] is True


def test_seam_graph_declared_seam_id_outside_the_graph_is_refused() -> None:
    with pytest.raises(ValueError, match=r"string_pattern_mismatch"):
        ModelSeamWindowClose.model_validate(
            {
                "seam_id": "VS-18",
                "side": "sender",
                "window": {
                    "start": "2026-11-02T12:00:00Z",
                    "end": "2026-11-02T12:10:00Z",
                },
                "reported_through": "2026-11-02T12:10:00Z",
                "crossing_ids": [],
            }
        )


def test_seam_graph_declared_new_nodes_exist_and_publish_the_count_topic() -> None:
    for name in ("node_seam_conservation_compute", "node_view_seam_reading_reducer"):
        assert (NODES / name / "contract.yaml").is_file(), name
        assert (NODES / name / "metadata.yaml").is_file(), name
    for name in SCAFFOLDS:
        contract = yaml.safe_load((NODES / name / "contract.yaml").read_text("utf-8"))
        assert COUNT_TOPIC in published_topics(contract), name


@pytest.mark.parametrize(("node", "handler"), sorted(SCAFFOLDS.items()))
def test_seam_graph_declared_scaffold_returns_a_typed_insufficient(
    node: str, handler: str
) -> None:
    module = importlib.import_module(
        f"omnimarket.nodes.{node}.handlers.handler_{node.removeprefix('node_')}"
    )
    result = getattr(module, handler)().handle(
        module.ModelSeamEmitterRequest(seam_id="VS-05", side="receiver")
    )
    assert result.status == "INSUFFICIENT"
    assert result.emitter == node
    assert "not built" in result.reason


def test_seam_graph_declared_every_unbuilt_emitter_has_an_insufficient_operation() -> (
    None
):
    from omnimarket.models.seam_reading.model_seam_emitter import (
        ModelSeamEmitterRequest,
    )

    for node in seam_nodes(load_graph()):
        declaration = ModelSeamDeclaration.from_graph_node(node)
        if declaration.status == "measured":
            continue
        for side, emitter in (
            ("sender", declaration.sender_emitter),
            ("receiver", declaration.receiver_emitter),
        ):
            contract = yaml.safe_load((NODES / emitter / "contract.yaml").read_text())
            entry = next(
                h
                for h in contract["handler_routing"]["handlers"]
                if h["operation"] == "emit_seam_count"
            )
            module = importlib.import_module(entry["handler"]["module"])
            result = getattr(module, entry["handler"]["name"])().handle(
                ModelSeamEmitterRequest(seam_id=declaration.seam_id, side=side)
            )
            assert result.status == "INSUFFICIENT"
            assert result.emitter == emitter
