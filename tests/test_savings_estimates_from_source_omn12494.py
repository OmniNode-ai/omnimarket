# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12494: materialize savings_estimates from canonical task-delegated SOURCE.

Closes the projection gap: savings_estimates is now materialized from the same
``onex.evt.omniclaude.task-delegated.v1`` stream that drives delegation_events,
using the measured actual cost (OMN-13355) and the pinned premium counterfactual
(OMN-13355) so the saving is a measurement. Idempotent on the identity index;
replay-deterministic; truthful-empty (no row) when there is no counterfactual.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelTaskDelegatedSavingsSource,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSavings()
CORR = "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"


def _source_payload(
    *, cost_usd: str = "0.003", with_cf: bool = True
) -> dict[str, object]:
    cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
    assert cf is not None
    payload: dict[str, object] = {
        "_event_type": "onex.evt.omniclaude.task-delegated.v1",
        "correlation_id": CORR,
        "task_type": "code_generation",
        "delegated_to": "cheap-cloud-glm",
        "model_name": "glm-5.2",
        "repo": "omnimarket",
        "cost_usd": cost_usd,
        "timestamp": "2026-06-20T12:00:00+00:00",
    }
    if with_cf:
        payload["premium_counterfactual"] = cf.model_dump(mode="json")
    return payload


@pytest.mark.unit
class TestSavingsSourceBuilder:
    def test_savings_is_counterfactual_minus_measured_actual(self) -> None:
        src = ModelTaskDelegatedSavingsSource.from_payload(_source_payload())
        proj = ModelDelegateSkillSavingsProjection.from_task_delegated_event(
            src, baseline_model="claude-opus-4-6"
        )
        assert proj is not None
        assert proj.local_cost_usd == Decimal("0.003")
        assert proj.cloud_cost_usd == Decimal("0.0525")
        assert proj.savings_usd == Decimal("0.0495")
        assert proj.model_cloud_baseline == "claude-opus-4-6"

    def test_no_counterfactual_yields_no_row(self) -> None:
        src = ModelTaskDelegatedSavingsSource.from_payload(
            _source_payload(with_cf=False)
        )
        assert (
            ModelDelegateSkillSavingsProjection.from_task_delegated_event(
                src, baseline_model="claude-opus-4-6"
            )
            is None
        )

    def test_nonpositive_saving_yields_no_row(self) -> None:
        # Actual cost >= counterfactual -> no defensible saving -> no row.
        src = ModelTaskDelegatedSavingsSource.from_payload(
            _source_payload(cost_usd="1.0")
        )
        assert (
            ModelDelegateSkillSavingsProjection.from_task_delegated_event(
                src, baseline_model="claude-opus-4-6"
            )
            is None
        )


@pytest.mark.unit
class TestHandlerMaterializesFromSource:
    def test_source_event_writes_savings_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle({**_source_payload(), "_db": db})
        assert result["rows_upserted"] == 1
        rows = db.query("savings_estimates", {"session_id": CORR})
        assert len(rows) == 1
        assert Decimal(str(rows[0]["local_cost_usd"])) == Decimal("0.003")
        assert Decimal(str(rows[0]["cloud_cost_usd"])) == Decimal("0.0525")
        assert Decimal(str(rows[0]["savings_usd"])) == Decimal("0.0495")
        assert rows[0]["model_local"] == "glm-5.2"
        assert rows[0]["repo_name"] == "omnimarket"

    def test_source_event_without_counterfactual_is_truthful_empty(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle({**_source_payload(with_cf=False), "_db": db})
        assert result["rows_upserted"] == 0
        assert db.query("savings_estimates", {"session_id": CORR}) == []

    def test_replay_is_idempotent(self) -> None:
        # Same source event applied twice yields exactly one row (identity index).
        db = InmemoryDatabaseAdapter()
        HANDLER.handle({**_source_payload(), "_db": db})
        HANDLER.handle({**_source_payload(), "_db": db})
        rows = db.query("savings_estimates", {"session_id": CORR})
        assert len(rows) == 1
        assert Decimal(str(rows[0]["savings_usd"])) == Decimal("0.0495")
