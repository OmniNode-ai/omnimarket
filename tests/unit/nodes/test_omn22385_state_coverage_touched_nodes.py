# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assert the declared outputs, topics and states of three nodes (OMN-22385).

Removing the omnibase-compat dependency from these nodes' ``metadata.yaml``
touches each node, and a touched node's contract-state coverage is checked
strictly. Their handler tests live under ``src/omnimarket/nodes/<node>/tests``,
which the coverage gate does not read, so the outputs, topics and FSM state
those contracts declare are asserted here against the real handlers and the
real ``contract.yaml`` files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_budget_action import EnumBudgetAction

from omnimarket.nodes.node_budget_policy_compute.handlers.handler_budget_policy import (
    HandlerBudgetPolicy,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits import (
    ModelBudgetLimits,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumTaskPriority,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_request import (
    ModelBudgetPolicyRequest,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_usage import (
    ModelBudgetUsage,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.handlers.handler_tool_reuse_matcher import (
    HandlerToolReuseMatcher,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_generated_tool import (
    ModelGeneratedToolRecord,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_enums import (
    EnumToolReuseMatchStrategy,
    EnumToolReuseVerdict,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_request import (
    ModelToolReuseRequest,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_signature import (
    ModelInputOutputSignature,
    compute_fields_hash,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.registry_in_memory import (
    InMemoryGeneratedToolRegistry,
)

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _contract(node: str) -> dict[str, object]:
    loaded = yaml.safe_load((_NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _published(node: str) -> list[str]:
    bus = _contract(node)["event_bus"]
    assert isinstance(bus, dict)
    topics = bus["publish_topics"]
    assert isinstance(topics, list)
    return topics


@pytest.mark.unit
def test_budget_policy_abort_names_reason_dimensions_and_recommended_action() -> None:
    request = ModelBudgetPolicyRequest(
        current_usage=ModelBudgetUsage(tokens=5000, cost_usd=0.0, elapsed_time_s=0.0),
        budget_limits=ModelBudgetLimits(
            max_tokens=1000, max_cost_usd=1.0, max_time_s=60.0
        ),
        task_priority=EnumTaskPriority.NORMAL,
    )

    result = HandlerBudgetPolicy().handle(request)

    assert result.action == EnumBudgetAction.ABORT
    assert result.reason.startswith("Hard limit exceeded on: tokens")
    assert result.dimensions_exceeded == ["tokens"]
    assert result.recommended_action == "Stop task immediately and release resources."


@pytest.mark.unit
def test_budget_policy_completed_topic_is_published() -> None:
    completed = "onex.evt.omnimarket.budget-policy-completed.v1"
    assert completed in _published("node_budget_policy_compute")
    assert _contract("node_budget_policy_compute")["terminal_event"] == completed


def _signature() -> ModelInputOutputSignature:
    return ModelInputOutputSignature(
        input_model_name="ModelScanRequest",
        input_model_module="omnimarket.generated.model_scan_request",
        output_model_name="ModelScanResult",
        output_model_module="omnimarket.generated.model_scan_result",
        input_fields_hash=compute_fields_hash({"source": "str"}),
        output_fields_hash=compute_fields_hash({"findings": "list"}),
    )


def _tool(tool_id: str) -> ModelGeneratedToolRecord:
    signature = _signature()
    return ModelGeneratedToolRecord(
        tool_id=tool_id,
        tool_name=f"node_generated_{tool_id}",
        handler_module=f"omnimarket.generated.{tool_id}.handler",
        handler_class="HandlerGenerated",
        contract_hash=f"sha256:{tool_id}",
        semantic_description="Scan source text and return findings",
        input_model_name=signature.input_model_name,
        output_model_name=signature.output_model_name,
        input_fields_hash=signature.input_fields_hash,
        output_fields_hash=signature.output_fields_hash,
        generated_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
    )


def _matcher(registry: object) -> HandlerToolReuseMatcher:
    container = MagicMock()
    container.get_service.return_value = registry
    return HandlerToolReuseMatcher(container=container)


def _request() -> ModelToolReuseRequest:
    return ModelToolReuseRequest(
        correlation_id=uuid4(),
        task_description="Scan source text and return findings",
        requested_signature=_signature(),
        match_strategy=EnumToolReuseMatchStrategy.SIGNATURE,
        similarity_threshold=0.85,
    )


@pytest.mark.unit
def test_tool_reuse_ambiguous_result_carries_candidates_and_strategy() -> None:
    matcher = _matcher(InMemoryGeneratedToolRegistry([_tool("alpha"), _tool("beta")]))

    result = matcher.handle(_request())

    assert result.verdict == EnumToolReuseVerdict.AMBIGUOUS
    assert sorted(c.tool.tool_id for c in result.candidate_tools) == ["alpha", "beta"]
    assert result.match_strategy_used == EnumToolReuseMatchStrategy.SIGNATURE
    assert result.failure_reason is None


@pytest.mark.unit
def test_tool_reuse_registry_failure_sets_failure_reason() -> None:
    class _Raising:
        def query_by_signature(self, **_: object) -> list[ModelGeneratedToolRecord]:
            raise RuntimeError("registry connection refused")

        def list_active(self) -> list[ModelGeneratedToolRecord]:
            raise RuntimeError("registry connection refused")

    result = _matcher(_Raising()).handle(_request())

    assert result.verdict == EnumToolReuseVerdict.REGISTRY_UNAVAILABLE
    assert result.failure_reason is not None
    assert "registry connection refused" in result.failure_reason


@pytest.mark.unit
def test_tool_reuse_matched_and_no_match_topics_are_published() -> None:
    published = _published("node_tool_reuse_matcher_compute")
    assert "onex.evt.omnimarket.tool-reuse-matched.v1" in published
    assert "onex.evt.omnimarket.tool-reuse-no-match.v1" in published


@pytest.mark.unit
def test_deployment_evidence_reducer_fsm_reaches_reduced_from_idle() -> None:
    machine = _contract("node_deployment_evidence_reducer")["state_machine"]
    assert isinstance(machine, dict)
    states = [s["state_name"] for s in machine["states"]]
    folded_from_idle = [
        t["to_state"] for t in machine["transitions"] if t["from_state"] == "idle"
    ]

    assert machine["initial_state"] == "idle"
    assert "reduced" in states
    assert "reduced" in folded_from_idle
