# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation workflow registry: 19 rows, every cell verified, nothing unread."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_seam_measure_compute.handlers.handler_load_delegation_workflows import (
    REGISTRY_PATH,
    HandlerLoadDelegationWorkflows,
    load_registry,
)
from omnimarket.nodes.node_delegation_seam_measure_compute.models.model_delegation_workflow import (
    ModelDelegationWorkflowRegistry,
    ModelLoadDelegationWorkflowsRequest,
)

NODE_DIR = REGISTRY_PATH.parent
NOW = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
UNREAD_PHRASE = "not " + "read"
CANARY_FAMILIES = {
    "canary_customer_local_and_plugin": {"W2", "W3", "W4", "W15"},
    "canary_lane_tooling": {"W5", "W6", "W7", "W8", "W9", "W11"},
    "canary_loops_composition_evaluation": {"W10", "W12", "W13", "W14", "W16", "W18"},
    "canary_cloud_and_coding_agent": {"W17", "W19"},
}


@pytest.mark.unit
def test_registry_holds_nineteen_rows_with_verified_cells() -> None:
    registry = load_registry()
    assert [w.id for w in registry.workflows] == [f"W{n}" for n in range(1, 20)]
    for workflow in registry.workflows:
        for cell in workflow.cells():
            assert cell.value.strip()
            assert cell.verified_by.strip(), workflow.id
            command, arrow, observation = cell.verified_by.partition(" => ")
            assert arrow, workflow.id
            assert command.strip(), workflow.id
            assert observation.strip(), workflow.id


@pytest.mark.unit
def test_registry_with_an_empty_cell_does_not_load() -> None:
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["workflows"][3]["receipt"]["verified_by"] = ""
    with pytest.raises(ValidationError):
        ModelDelegationWorkflowRegistry.model_validate(raw)
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["workflows"][3]["receipt"]["verified_by"] = "rg receipt"
    with pytest.raises(ValidationError):
        ModelDelegationWorkflowRegistry.model_validate(raw)


@pytest.mark.unit
def test_registry_with_a_missing_row_does_not_load(tmp_path: Path) -> None:
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["workflows"].pop()
    short = tmp_path / "short.yaml"
    short.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="W1 to W19"):
        load_registry(short)


@pytest.mark.unit
def test_no_cell_says_unread() -> None:
    text = REGISTRY_PATH.read_text(encoding="utf-8").lower()
    assert UNREAD_PHRASE not in text
    assert "unverified" not in text


@pytest.mark.unit
def test_registry_names_every_station_shape() -> None:
    registry = load_registry()
    assert [s.station for s in registry.station_shapes] == [
        f"DSt{n}" for n in range(1, 9)
    ]
    dst3 = registry.station_shapes[2]
    assert "ModelInferenceIntent" in dst3.shape.value


@pytest.mark.unit
def test_every_row_has_one_measurement_a_canary_and_an_owner() -> None:
    registry = load_registry()
    by_family: dict[str, set[str]] = {}
    for workflow in registry.workflows:
        assert workflow.measurement in workflow.seams, workflow.id
        assert workflow.canary.ref.strip(), workflow.id
        assert workflow.owner.strip(), workflow.id
        by_family.setdefault(workflow.canary.family, set()).add(workflow.id)
    for family, ids in CANARY_FAMILIES.items():
        assert by_family.get(family) == ids, family


@pytest.mark.unit
def test_w18_callers_are_traced_to_a_canary_family() -> None:
    w18 = next(w for w in load_registry().workflows if w.id == "W18")
    assert w18.canary.family == "canary_loops_composition_evaluation"
    assert "node_architecture_discovery_orchestrator" in w18.entry.value


@pytest.mark.unit
def test_registry_holds_no_private_or_deployment_name() -> None:
    text = REGISTRY_PATH.read_text(encoding="utf-8")
    needles = (
        "/" + "Users/",
        "/" + "Volumes/",
        "192." + "168.",
        "OMN" + "-",
        "linear" + ".app",
        "omni" + "_home",
    )
    for needle in needles:
        assert needle not in text, needle


@pytest.mark.unit
def test_contract_declares_no_protocol_dependencies() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert "protocol_dependencies" not in contract
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["node_type"] == "compute"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-workflows-loaded.v1"
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.delegation-workflows-load-requested.v1"
    )


@pytest.mark.unit
def test_handler_returns_the_rows_and_stamps_now() -> None:
    handler = HandlerLoadDelegationWorkflows()
    result = handler.handle(ModelLoadDelegationWorkflowsRequest(now=NOW))
    assert result.read_at == NOW
    assert len(result.workflows) == 19
    assert result == handler.handle(ModelLoadDelegationWorkflowsRequest(now=NOW))
    only = handler.handle(
        ModelLoadDelegationWorkflowsRequest(now=NOW, workflow_ids=("W7", "W8"))
    )
    assert [w.id for w in only.workflows] == ["W7", "W8"]
    with pytest.raises(ValueError, match="unknown workflow ids"):
        handler.handle(
            ModelLoadDelegationWorkflowsRequest(now=NOW, workflow_ids=("W20",))
        )


@pytest.mark.unit
def test_orchestrator_contract_has_the_edge_to_this_node() -> None:
    contract = yaml.safe_load(
        (
            NODE_DIR.parent / "node_delegation_seam_orchestrator" / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert "node_delegation_seam_measure_compute" in {
        edge["to"] for edge in contract["edges"]
    }
