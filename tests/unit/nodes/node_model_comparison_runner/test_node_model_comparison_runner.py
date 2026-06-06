# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_model_comparison_runner.

Tests cover:
- Contract YAML structure and topic naming
- Handler fan-out logic: winner selection, error propagation, empty cells
- Models: immutability, default field values
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from omnimarket.nodes.node_model_comparison_runner.handlers.handler_model_comparison import (
    HandlerModelComparisonRunner,
    _pick_winner,
)
from omnimarket.nodes.node_model_comparison_runner.models.model_comparison_request import (
    ModelComparisonRequest,
    ModelEndpointSpec,
)
from omnimarket.nodes.node_model_comparison_runner.models.model_comparison_result import (
    ModelComparisonCell,
    ModelComparisonResult,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_model_comparison_runner"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_yaml_is_well_formed() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["name"] == "node_model_comparison_runner"
    assert data["node_type"] == "effect"
    assert isinstance(data["contract_version"], dict)
    assert data["contract_version"]["major"] == 1


@pytest.mark.unit
def test_contract_declares_expected_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    bus = data["event_bus"]
    assert (
        "onex.cmd.omnimarket.model-comparison-requested.v1" in bus["subscribe_topics"]
    )
    assert "onex.evt.omnimarket.model-comparison-completed.v1" in bus["publish_topics"]


@pytest.mark.unit
def test_contract_terminal_event_matches_publish_topic() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    terminal = data["terminal_event"]
    published = data["event_bus"]["publish_topics"]
    assert terminal in published, (
        f"terminal_event '{terminal}' must appear in event_bus.publish_topics"
    )


@pytest.mark.unit
def test_contract_descriptor_is_effect() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    desc = data["descriptor"]
    assert desc["node_archetype"] == "effect"
    assert desc["purity"] == "impure"
    assert desc["idempotent"] is False


@pytest.mark.unit
def test_contract_handler_routing_present() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    routing = data["handler_routing"]
    handlers = routing["handlers"]
    assert len(handlers) >= 1
    entry = handlers[0]
    assert entry["handler"]["name"] == "HandlerModelComparisonRunner"
    assert "node_model_comparison_runner" in entry["handler"]["module"]


# ---------------------------------------------------------------------------
# _pick_winner tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pick_winner_no_cells_returns_none() -> None:
    assert _pick_winner([]) is None


@pytest.mark.unit
def test_pick_winner_all_errors_returns_none() -> None:
    cells = [
        ModelComparisonCell(model_id="a", label="A", provider="p", error="oops"),
        ModelComparisonCell(model_id="b", label="B", provider="p", error="fail"),
    ]
    assert _pick_winner(cells) is None


@pytest.mark.unit
def test_pick_winner_single_success() -> None:
    cells = [
        ModelComparisonCell(
            model_id="a", label="Alpha", provider="p", total_tokens=100, cost_usd=0.01
        ),
    ]
    assert _pick_winner(cells) == "Alpha"


@pytest.mark.unit
def test_pick_winner_prefers_fewer_tokens() -> None:
    cells = [
        ModelComparisonCell(
            model_id="a",
            label="Expensive",
            provider="p",
            total_tokens=500,
            cost_usd=0.01,
        ),
        ModelComparisonCell(
            model_id="b", label="Cheap", provider="p", total_tokens=100, cost_usd=0.05
        ),
    ]
    assert _pick_winner(cells) == "Cheap"


@pytest.mark.unit
def test_pick_winner_tiebreak_by_cost() -> None:
    cells = [
        ModelComparisonCell(
            model_id="a", label="Costly", provider="p", total_tokens=100, cost_usd=0.10
        ),
        ModelComparisonCell(
            model_id="b", label="Budget", provider="p", total_tokens=100, cost_usd=0.01
        ),
    ]
    assert _pick_winner(cells) == "Budget"


@pytest.mark.unit
def test_pick_winner_skips_error_cells() -> None:
    cells = [
        ModelComparisonCell(
            model_id="a",
            label="Bad",
            provider="p",
            total_tokens=10,
            error="network err",
        ),
        ModelComparisonCell(
            model_id="b", label="Good", provider="p", total_tokens=200, cost_usd=0.0
        ),
    ]
    assert _pick_winner(cells) == "Good"


# ---------------------------------------------------------------------------
# Handler tests (injected mock LLM effect)
# ---------------------------------------------------------------------------


def _make_fake_inference_response(
    tokens_input: int = 10,
    tokens_output: int = 20,
    latency_ms: float = 50.0,
) -> Any:
    """Build a duck-typed object that mimics ModelLlmInferenceResponse."""

    class FakeUsage:
        pass

    usage = FakeUsage()
    usage.tokens_input = tokens_input  # type: ignore[attr-defined]
    usage.tokens_output = tokens_output  # type: ignore[attr-defined]
    usage.tokens_total = tokens_input + tokens_output  # type: ignore[attr-defined]

    class FakeResponse:
        pass

    resp = FakeResponse()
    resp.usage = usage  # type: ignore[attr-defined]
    resp.latency_ms = latency_ms  # type: ignore[attr-defined]
    return resp


def _make_request(
    *labels: str,
    task: str = "write a hello world function",
) -> ModelComparisonRequest:
    specs = tuple(
        ModelEndpointSpec(
            model_id=f"model-{label.lower()}",
            endpoint="http://localhost:8000",
            provider="local",
            label=label,
        )
        for label in labels
    )
    return ModelComparisonRequest(task_description=task, models=specs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_returns_result_with_all_cells() -> None:
    mock_effect = AsyncMock()
    mock_effect.handle.return_value = _make_fake_inference_response()

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = _make_request("Alpha", "Beta")
    result = await handler.handle(request)

    assert isinstance(result, ModelComparisonResult)
    assert len(result.cells) == 2
    labels = {c.label for c in result.cells}
    assert labels == {"Alpha", "Beta"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_winner_label_set() -> None:
    mock_effect = AsyncMock()
    mock_effect.handle.return_value = _make_fake_inference_response(
        tokens_input=5, tokens_output=5
    )

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = _make_request("OnlyModel")
    result = await handler.handle(request)

    assert result.winner_label == "OnlyModel"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_error_cell_on_exception() -> None:
    mock_effect = AsyncMock()
    mock_effect.handle.side_effect = RuntimeError("connection refused")

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = _make_request("FailModel")
    result = await handler.handle(request)

    assert len(result.cells) == 1
    cell = result.cells[0]
    assert cell.error != ""
    assert result.winner_label is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_partial_failure_winner_from_success() -> None:
    ok_response = _make_fake_inference_response(tokens_input=10, tokens_output=10)

    call_count = 0

    async def handle_side_effect(req: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("timeout")
        return ok_response

    mock_effect = AsyncMock()
    mock_effect.handle = AsyncMock(side_effect=handle_side_effect)

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = _make_request("Fail", "Success")
    result = await handler.handle(request)

    assert result.winner_label == "Success"
    error_cell = next(c for c in result.cells if c.label == "Fail")
    assert error_cell.error != ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_is_immutable() -> None:
    mock_effect = AsyncMock()
    mock_effect.handle.return_value = _make_fake_inference_response()

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = _make_request("M1")
    result = await handler.handle(request)

    import pydantic

    with pytest.raises((pydantic.ValidationError, TypeError)):
        result.comparison_id = "overwritten"  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_comparison_id_is_unique_per_run() -> None:
    mock_effect = AsyncMock()
    mock_effect.handle.return_value = _make_fake_inference_response()

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = _make_request("M1")
    r1 = await handler.handle(request)
    r2 = await handler.handle(request)

    assert r1.comparison_id != r2.comparison_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_winner_criteria_propagated() -> None:
    mock_effect = AsyncMock()
    mock_effect.handle.return_value = _make_fake_inference_response()

    handler = HandlerModelComparisonRunner(effect_handler=mock_effect)
    request = ModelComparisonRequest(
        task_description="task",
        models=(
            ModelEndpointSpec(
                model_id="m",
                endpoint="http://localhost",
                provider="local",
                label="L",
            ),
        ),
        winner_criteria="custom_criteria",
    )
    result = await handler.handle(request)
    assert result.winner_criteria == "custom_criteria"
