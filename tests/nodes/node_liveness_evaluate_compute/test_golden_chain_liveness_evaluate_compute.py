# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain / unit tests for node_liveness_evaluate_compute (OMN-15126).

Proves the pure state decision (design §3.2) for all 5 states from
constructed, deterministic input. Zero I/O -- every case is a pure
function-in/model-out assertion. The live-Postgres proof of the same
pipeline (steps 1-3, real demand-query + correlated join) lives in
tests/nodes/node_liveness_demand_query_effect/
test_acceptance_liveness_evaluator_chain_omn15126.py.

Related:
    - OMN-15126: demand-aware liveness evaluator
    - OMN-14845: design (docs/design/2026-07-20-demand-aware-liveness-state-machine-design.md)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from omnibase_core.models.runtime.model_event_ref import ModelEventRef

from omnimarket.nodes.node_liveness_evaluate_compute.handlers.handler_liveness_evaluate_compute import (
    HandlerLivenessEvaluateCompute,
)
from omnimarket.nodes.node_liveness_evaluate_compute.models.model_liveness_evaluate_request import (
    ModelLivenessEvaluateRequest,
)

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def _base_request(**overrides: object) -> ModelLivenessEvaluateRequest:
    defaults: dict[str, object] = {
        "surface_id": "omnimarket.test_surface",
        "lane": "dev",
        "deployed_sha": "abc123",
        "image_digest": "sha256:deadbeef",
        "config_digest": "sha256:cafef00d",
        "runner": "node_liveness_evaluate_compute@test",
        "evaluated_at": _NOW,
        "freshness_window_seconds": 300,
        "error_budget_ratio": 0.0,
    }
    defaults.update(overrides)
    return ModelLivenessEvaluateRequest(**defaults)


@pytest.mark.unit
class TestLivenessEvaluateComputeGoldenChain:
    """All 5 states, pure decision from constructed input."""

    async def test_registry_unresolved_is_not_ready(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        request = _base_request(
            registry_resolved=False,
            not_ready_reason="registry catalog lookup failed for surface_id",
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.NOT_READY
        assert (
            receipt.not_ready_reason == "registry catalog lookup failed for surface_id"
        )
        assert receipt.correlation_id is None
        assert receipt.checked_count is None

    async def test_demand_query_failure_is_not_ready(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        request = _base_request(
            demand_query_succeeded=False,
            not_ready_reason="demand query failed: relation does not exist",
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.NOT_READY
        assert "relation does not exist" in (receipt.not_ready_reason or "")

    async def test_eligible_demand_all_correlated_is_healthy(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        correlation_id = uuid4()
        input_ref = ModelEventRef(
            topic="onex.cmd.test.input.v1", partition=0, offset=1, event_id=uuid4()
        )
        terminal_ref = ModelEventRef(
            topic="onex.evt.test.terminal.v1", partition=0, offset=2, event_id=uuid4()
        )
        request = _base_request(
            eligible_count=3,
            checked_count=3,
            failed_count=0,
            correlation_id=correlation_id,
            input_event_ref=input_ref,
            terminal_event_ref=terminal_ref,
            projection_key_canonical='{"correlation_id": "x"}',
            projection_value_hash="hash-observed",
            projection_expected_value_hash="hash-expected",
            expected_value_predicate_result=True,
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.HEALTHY
        assert receipt.correlation_id == correlation_id
        assert receipt.checked_count == 3
        assert receipt.failed_count == 0
        assert receipt.failed_ratio == 0.0
        assert receipt.terminal_event_ref == terminal_ref

    async def test_failed_ratio_exceeds_budget_is_red(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        correlation_id = uuid4()
        input_ref = ModelEventRef(
            topic="onex.cmd.test.input.v1", partition=0, offset=1, event_id=uuid4()
        )
        request = _base_request(
            eligible_count=2,
            checked_count=2,
            failed_count=1,
            correlation_id=correlation_id,
            input_event_ref=input_ref,
            projection_key_canonical='{"correlation_id": "x"}',
            expected_value_predicate_result=False,
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.RED
        assert receipt.checked_count == 2
        assert receipt.failed_count == 1
        assert receipt.failed_ratio == pytest.approx(0.5)
        assert receipt.terminal_event_ref is None
        assert receipt.failure_detail is not None
        assert "exceeds" in receipt.failure_detail

    async def test_nonzero_error_budget_absorbs_failure_as_healthy(self) -> None:
        """A registry-declared nonzero error_budget_ratio permits a bounded failure rate."""
        handler = HandlerLivenessEvaluateCompute()
        request = _base_request(
            error_budget_ratio=0.5,
            eligible_count=4,
            checked_count=4,
            failed_count=1,
            correlation_id=uuid4(),
            input_event_ref=ModelEventRef(
                topic="onex.cmd.test.input.v1", partition=0, offset=1, event_id=uuid4()
            ),
            terminal_event_ref=ModelEventRef(
                topic="onex.evt.test.terminal.v1",
                partition=0,
                offset=2,
                event_id=uuid4(),
            ),
            projection_key_canonical='{"correlation_id": "x"}',
            projection_value_hash="hash-observed",
            projection_expected_value_hash="hash-expected",
            expected_value_predicate_result=True,
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.HEALTHY
        assert receipt.failed_ratio == pytest.approx(0.25)

    async def test_zero_eligible_with_fresh_prior_healthy_is_no_demand(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        prior_receipt_id = uuid4()
        request = _base_request(
            eligible_count=0,
            demand_query_evidence="query_table=event_ledger row_count=0",
            prior_healthy_receipt_id=prior_receipt_id,
            prior_healthy_at=_NOW - timedelta(seconds=60),
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.NO_DEMAND
        assert receipt.demand_query_evidence == "query_table=event_ledger row_count=0"
        assert receipt.last_healthy_receipt_id is None

    async def test_zero_eligible_never_healthy_is_stale(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        request = _base_request(eligible_count=0)

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.STALE
        assert receipt.last_healthy_receipt_id is None
        assert receipt.last_healthy_at is None

    async def test_zero_eligible_with_stale_prior_healthy_is_stale(self) -> None:
        handler = HandlerLivenessEvaluateCompute()
        prior_receipt_id = uuid4()
        prior_at = _NOW - timedelta(
            seconds=600
        )  # older than freshness_window_seconds=300
        request = _base_request(
            eligible_count=0,
            prior_healthy_receipt_id=prior_receipt_id,
            prior_healthy_at=prior_at,
        )

        receipt = await handler.handle(request)

        assert receipt.state == EnumLivenessState.STALE
        assert receipt.last_healthy_receipt_id == prior_receipt_id
        assert receipt.last_healthy_at == prior_at

    async def test_prior_healthy_pairing_is_enforced_on_the_request(self) -> None:
        """A request with one of the paired prior-healthy fields set but not the
        other is rejected at construction (never silently dropped)."""
        with pytest.raises(ValueError, match="must be provided together"):
            _base_request(
                eligible_count=0,
                prior_healthy_receipt_id=uuid4(),
                prior_healthy_at=None,
            )
