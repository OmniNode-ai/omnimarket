"""Golden-chain guardrails for newly added explicit stub nodes.

These nodes are intentionally not implemented yet. The golden chain for this
slice is honest routing behavior: contracts mark the node as not implemented,
typed models are strict, registry entry points load, and handlers fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError


@dataclass(frozen=True)
class StubNodeCase:
    node_name: str
    handler_module: str
    handler_class: str
    request_module: str
    request_class: str
    result_module: str
    result_class: str


STUB_NODE_CASES: tuple[StubNodeCase, ...] = (
    StubNodeCase(
        node_name="node_dod_sweep_orchestrator",
        handler_module="omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator",
        handler_class="HandlerDodSweepOrchestrator",
        request_module="omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request",
        request_class="ModelDodSweepOrchestratorRequest",
        result_module="omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_result",
        result_class="ModelDodSweepOrchestratorResult",
    ),
    StubNodeCase(
        node_name="node_env_parity_compute",
        handler_module="omnimarket.nodes.node_env_parity_compute.handlers.handler_env_parity_compute",
        handler_class="HandlerEnvParityCompute",
        request_module="omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_request",
        request_class="ModelEnvParityComputeRequest",
        result_module="omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_result",
        result_class="ModelEnvParityComputeResult",
    ),
    StubNodeCase(
        node_name="node_gap_compute",
        handler_module="omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute",
        handler_class="HandlerGapCompute",
        request_module="omnimarket.nodes.node_gap_compute.models.model_gap_compute_request",
        request_class="ModelGapComputeRequest",
        result_module="omnimarket.nodes.node_gap_compute.models.model_gap_compute_result",
        result_class="ModelGapComputeResult",
    ),
    StubNodeCase(
        node_name="node_pr_watch_orchestrator",
        handler_module="omnimarket.nodes.node_pr_watch_orchestrator.handlers.handler_pr_watch_orchestrator",
        handler_class="HandlerPrWatchOrchestrator",
        request_module="omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_watch_orchestrator_request",
        request_class="ModelPrWatchOrchestratorRequest",
        result_module="omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_watch_orchestrator_result",
        result_class="ModelPrWatchOrchestratorResult",
    ),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_attr(module_name: str, attr_name: str) -> Any:
    return getattr(import_module(module_name), attr_name)


@pytest.mark.unit
@pytest.mark.parametrize("case", STUB_NODE_CASES, ids=lambda case: case.node_name)
def test_stub_node_contract_is_explicit(case: StubNodeCase) -> None:
    contract_path = (
        _repo_root() / "src" / "omnimarket" / "nodes" / case.node_name / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is True
    assert raw["handler"]["module"] == case.handler_module
    assert raw["handler"]["class"] == case.handler_class
    assert (
        raw["handler"]["input_model"] == f"{case.request_module}.{case.request_class}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", STUB_NODE_CASES, ids=lambda case: case.node_name)
def test_stub_node_entry_point_loads(case: StubNodeCase) -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[case.node_name].load()

    assert loaded.__name__ == f"omnimarket.nodes.{case.node_name}"


@pytest.mark.unit
@pytest.mark.parametrize("case", STUB_NODE_CASES, ids=lambda case: case.node_name)
def test_stub_node_models_are_strict(case: StubNodeCase) -> None:
    request_model = _load_attr(case.request_module, case.request_class)
    result_model = _load_attr(case.result_module, case.result_class)

    request = request_model(scope="local")
    result = result_model(status="not_implemented")

    assert request.scope == "local"
    assert result.status == "not_implemented"
    with pytest.raises(ValidationError):
        request_model(scope="local", unexpected=True)
    with pytest.raises(ValidationError):
        result_model(status="not_implemented", unexpected=True)


@pytest.mark.unit
@pytest.mark.parametrize("case", STUB_NODE_CASES, ids=lambda case: case.node_name)
def test_stub_node_handler_fails_loudly(case: StubNodeCase) -> None:
    handler_type = _load_attr(case.handler_module, case.handler_class)
    request_model = _load_attr(case.request_module, case.request_class)

    with pytest.raises(NotImplementedError, match="node_not_implemented"):
        handler_type().handle(request_model(scope="local"))
