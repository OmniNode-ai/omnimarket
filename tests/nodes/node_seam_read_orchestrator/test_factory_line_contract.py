# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_read_orchestrator: the factory line graph contract (OMN-21459)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.contracts.model_contract_workflow_definition import (
    ModelContractWorkflowDefinition,
)
from omnibase_core.models.contracts.subcontracts.model_protocol_dependency import (
    ModelProtocolDependency,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "src/omnimarket/nodes/node_seam_read_orchestrator/contract.yaml"
FRAGMENT = Path(__file__).parent / "fixtures" / "factory_line_fragment.yaml"


def _load(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _definition(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["workflow_coordination"]["workflow_definition"]


def _nodes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _definition(raw)["execution_graph"]["nodes"]


def _seams(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in _nodes(raw) if n["node_type"] == "COMPUTE_GENERIC"]


def test_the_graph_parses_with_the_core_model_and_has_the_counts() -> None:
    definition = ModelContractWorkflowDefinition.model_validate(
        _definition(_load(CONTRACT))
    )
    nodes = definition.execution_graph.nodes
    assert len(nodes) == 28
    assert sum(len(n.depends_on) for n in nodes) == 27
    assert sum(1 for n in nodes if n.node_type == "COMPUTE_GENERIC") == 14
    assert sum(1 for n in nodes if n.node_type == "EFFECT_GENERIC") == 14


def test_each_seam_has_a_shape_or_a_gap_and_a_classify_operation() -> None:
    seams = _seams(_load(CONTRACT))
    assert len(seams) == 14
    for seam in seams:
        step = seam["step_config"]
        assert "shape" in step or "gap" in step, seam["node_id"]
        assert step["operation"].startswith("classify_sm_"), seam["node_id"]


def test_a_station_carries_no_operation() -> None:
    stations = [
        n for n in _nodes(_load(CONTRACT)) if n["node_type"] == "EFFECT_GENERIC"
    ]
    assert len(stations) == 14
    assert all("step_config" not in n for n in stations)


def test_the_graph_equals_the_plan_fragment() -> None:
    assert _definition(_load(CONTRACT)) == _definition(_load(FRAGMENT))


def test_a_changed_seam_id_fails_the_comparison() -> None:
    raw = _load(CONTRACT)
    _nodes(raw)[1]["node_id"] = "sm_00_renamed"
    assert _definition(raw) != _definition(_load(FRAGMENT))


def test_the_seam_read_config_carries_the_register_values() -> None:
    config = _load(CONTRACT)["config"]["seam_read"]
    assert config["default_minimum_crossings"] == 20
    assert config["consecutive_clean_windows"] == 2
    assert config["window_hours"] == 24


def test_the_orchestrator_declares_bus_and_clock_exactly() -> None:
    deps = [
        ModelProtocolDependency.model_validate(d)
        for d in _load(CONTRACT)["protocol_dependencies"]
    ]
    assert [d.name for d in deps] == ["BUS", "CLK"]
