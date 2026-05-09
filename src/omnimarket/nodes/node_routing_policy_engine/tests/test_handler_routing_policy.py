# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerRoutingPolicy — deterministic exploit/explore model selection."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_routing_policy_engine.handlers.handler_routing_policy import (
    HandlerRoutingPolicy,
)
from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_request import (
    EnumCapabilityRequirement,
    EnumTaskType,
    ModelAvailableModel,
    ModelRoutingPolicyRequest,
)
from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_result import (
    EnumRoutingStatus,
    EnumSelectionMode,
)


def _model(
    key: str,
    score: float = 0.8,
    cost: float = 0.001,
    capabilities: frozenset[EnumCapabilityRequirement] = frozenset(),
) -> ModelAvailableModel:
    return ModelAvailableModel(
        key=key,
        score=score,
        cost_per_token=cost,
        capabilities=capabilities,
    )


def _request(
    models: tuple[ModelAvailableModel, ...] | None = None,
    task_type: EnumTaskType = EnumTaskType.CODE,
    required_capabilities: frozenset[EnumCapabilityRequirement] = frozenset(),
    max_cost_per_token: float | None = None,
    exploration_seed: float = 0.0,
    exploration_rate: float = 0.15,
    request_id: str = "req-001",
) -> ModelRoutingPolicyRequest:
    if models is None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
        )
    return ModelRoutingPolicyRequest(
        task_type=task_type,
        required_capabilities=required_capabilities,
        available_models=models,
        max_cost_per_token=max_cost_per_token,
        exploration_seed=exploration_seed,
        exploration_rate=exploration_rate,
        request_id=request_id,
    )


@pytest.mark.unit
class TestHandlerRoutingPolicyExploit:
    def test_status_ok_on_success(self) -> None:
        result = HandlerRoutingPolicy().handle(_request())
        assert result.status == EnumRoutingStatus.OK

    def test_selects_highest_scoring_model_in_exploit(self) -> None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
            _model("model-c", score=0.5),
        )
        req = _request(models=models, exploration_seed=0.99)
        result = HandlerRoutingPolicy().handle(req)
        assert result.selected_model_key == "model-a"
        assert result.selection_mode == EnumSelectionMode.EXPLOIT

    def test_exploit_when_seed_equals_rate(self) -> None:
        # seed == rate means NOT less-than, so exploit path
        req = _request(exploration_seed=0.15, exploration_rate=0.15)
        result = HandlerRoutingPolicy().handle(req)
        assert result.selection_mode == EnumSelectionMode.EXPLOIT

    def test_alternatives_ranked_descending_in_exploit(self) -> None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
            _model("model-c", score=0.5),
        )
        req = _request(models=models, exploration_seed=0.99)
        result = HandlerRoutingPolicy().handle(req)
        assert len(result.alternative_candidates) == 2
        assert result.alternative_candidates[0].key == "model-b"
        assert result.alternative_candidates[0].rank == 1
        assert result.alternative_candidates[1].key == "model-c"
        assert result.alternative_candidates[1].rank == 2

    def test_request_id_echoed(self) -> None:
        req = _request(request_id="corr-xyz")
        result = HandlerRoutingPolicy().handle(req)
        assert result.request_id == "corr-xyz"

    def test_no_error_on_success(self) -> None:
        result = HandlerRoutingPolicy().handle(_request())
        assert result.error is None

    def test_selection_reason_contains_model_key(self) -> None:
        req = _request(exploration_seed=0.99)
        result = HandlerRoutingPolicy().handle(req)
        assert "model-a" in result.selection_reason


@pytest.mark.unit
class TestHandlerRoutingPolicyExplore:
    def test_selects_second_best_in_explore(self) -> None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
        )
        req = _request(models=models, exploration_seed=0.05, exploration_rate=0.15)
        result = HandlerRoutingPolicy().handle(req)
        assert result.selected_model_key == "model-b"
        assert result.selection_mode == EnumSelectionMode.EXPLORE

    def test_explore_alternatives_include_best_model(self) -> None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
            _model("model-c", score=0.5),
        )
        req = _request(models=models, exploration_seed=0.05, exploration_rate=0.15)
        result = HandlerRoutingPolicy().handle(req)
        keys = [c.key for c in result.alternative_candidates]
        assert "model-a" in keys

    def test_no_explore_when_only_one_model(self) -> None:
        # With a single eligible model, exploration cannot pick second-best
        req = _request(
            models=(_model("only-model"),),
            exploration_seed=0.01,
            exploration_rate=0.15,
        )
        result = HandlerRoutingPolicy().handle(req)
        assert result.selected_model_key == "only-model"
        assert result.selection_mode == EnumSelectionMode.EXPLOIT

    def test_selection_reason_contains_seed_and_rate(self) -> None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
        )
        req = _request(models=models, exploration_seed=0.05, exploration_rate=0.15)
        result = HandlerRoutingPolicy().handle(req)
        assert "0.0500" in result.selection_reason
        assert "0.1500" in result.selection_reason


@pytest.mark.unit
class TestHandlerRoutingPolicyCostFilter:
    def test_excludes_models_above_cost_ceiling(self) -> None:
        models = (
            _model("expensive", score=0.95, cost=0.01),
            _model("cheap", score=0.7, cost=0.001),
        )
        req = _request(models=models, max_cost_per_token=0.005, exploration_seed=0.99)
        result = HandlerRoutingPolicy().handle(req)
        assert result.selected_model_key == "cheap"

    def test_error_when_all_models_exceed_cost(self) -> None:
        models = (
            _model("expensive-a", score=0.9, cost=0.1),
            _model("expensive-b", score=0.8, cost=0.2),
        )
        req = _request(models=models, max_cost_per_token=0.001)
        result = HandlerRoutingPolicy().handle(req)
        assert result.status == EnumRoutingStatus.ERROR
        assert result.error is not None

    def test_allows_model_exactly_at_cost_ceiling(self) -> None:
        models = (_model("at-ceiling", score=0.8, cost=0.005),)
        req = _request(models=models, max_cost_per_token=0.005)
        result = HandlerRoutingPolicy().handle(req)
        assert result.status == EnumRoutingStatus.OK
        assert result.selected_model_key == "at-ceiling"


@pytest.mark.unit
class TestHandlerRoutingPolicyCapabilityFilter:
    def test_excludes_models_missing_required_capability(self) -> None:
        has_cap = _model(
            "capable",
            score=0.7,
            capabilities=frozenset({EnumCapabilityRequirement.CODE_GENERATION}),
        )
        no_cap = _model("incapable", score=0.95, capabilities=frozenset())
        req = _request(
            models=(no_cap, has_cap),
            required_capabilities=frozenset(
                {EnumCapabilityRequirement.CODE_GENERATION}
            ),
            exploration_seed=0.99,
        )
        result = HandlerRoutingPolicy().handle(req)
        assert result.selected_model_key == "capable"

    def test_error_when_no_model_satisfies_capabilities(self) -> None:
        req = _request(
            models=(_model("no-cap", capabilities=frozenset()),),
            required_capabilities=frozenset({EnumCapabilityRequirement.TOOL_USE}),
        )
        result = HandlerRoutingPolicy().handle(req)
        assert result.status == EnumRoutingStatus.ERROR

    def test_model_with_superset_of_capabilities_is_eligible(self) -> None:
        all_caps = frozenset(EnumCapabilityRequirement)
        rich_model = _model("rich", score=0.8, capabilities=all_caps)
        req = _request(
            models=(rich_model,),
            required_capabilities=frozenset({EnumCapabilityRequirement.REASONING}),
        )
        result = HandlerRoutingPolicy().handle(req)
        assert result.status == EnumRoutingStatus.OK
        assert result.selected_model_key == "rich"


@pytest.mark.unit
class TestHandlerRoutingPolicyDeterminism:
    def test_same_input_same_output(self) -> None:
        req = _request()
        handler = HandlerRoutingPolicy()
        assert (
            handler.handle(req).selected_model_key
            == handler.handle(req).selected_model_key
        )

    def test_different_seeds_can_produce_different_selections(self) -> None:
        models = (
            _model("model-a", score=0.9),
            _model("model-b", score=0.7),
        )
        exploit_req = _request(models=models, exploration_seed=0.99)
        explore_req = _request(
            models=models, exploration_seed=0.01, exploration_rate=0.15
        )
        handler = HandlerRoutingPolicy()
        assert (
            handler.handle(exploit_req).selected_model_key
            != handler.handle(explore_req).selected_model_key
        )

    def test_score_ordering_determines_selection(self) -> None:
        models = (
            _model("low", score=0.3),
            _model("high", score=0.95),
            _model("mid", score=0.6),
        )
        req = _request(models=models, exploration_seed=0.99)
        result = HandlerRoutingPolicy().handle(req)
        assert result.selected_model_key == "high"
