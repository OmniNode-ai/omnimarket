# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20303 AC2: a run's delegation_events row keeps the baseline its saving came from.

The delegate-skill terminal carries the pinned premium counterfactual, the
session and the delegating actor. The canonical delegation-completed terminal
for the same correlation carries none of them, and when it is written second it
named all three empty and overwrote them, while ``cost_savings_usd`` survived
through the preserve step. On the .201 dev lane 211 rows carried a non-zero
saving with no counterfactual and an empty delegated_by, so the dashboard showed
a saving it could not show the baseline for.

The attribution fold keeps a stored value whenever the incoming terminal states
none; a terminal that carries its own value still wins. Both writers -- the sync
handler and the deployed async runner -- apply the same fold in their preserve
step.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_run_attribution_fold import (
    HandlerDelegationRunAttributionFold,
    ModelDelegationRunAttributionFoldRequest,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

RUN = "727c0456-1758-421a-8600-25da6410f2cf"
SESSION = "630e98d8-b59c-4fe2-8c4d-8cacdd1dd30b"


def _counterfactual() -> dict[str, object]:
    cf = build_premium_counterfactual(prompt_tokens=319, completion_tokens=154)
    assert cf is not None
    return cf.model_dump(mode="json")


def _delegate_skill_payload() -> dict[str, object]:
    cf = _counterfactual()
    return {
        "_event_type": "delegate-skill-completed",
        "status": "completed",
        "correlation_id": RUN,
        "session_id": SESSION,
        "task_type": "summarization",
        "provider": "local",
        "model_name": "Qwen3.8-27B",
        "response": "an answer",
        "quality_gate_passed": True,
        "quality_gates_failed": [],
        "emitted_at": "2026-10-01T12:23:58.700000+00:00",
        "metrics": {
            "input_tokens": 319,
            "output_tokens": 154,
            "total_tokens": 473,
            "latency_ms": 900,
            "cost_usd": 0.0,
            "cost_savings_usd": float(str(cf["counterfactual_cost_usd"])),
            "premium_counterfactual": cf,
        },
    }


def _canonical_payload() -> dict[str, object]:
    return {
        "_event_type": "onex.evt.omnibase-infra.delegation-completed.v1",
        "correlation_id": RUN,
        "task_type": "summarization",
        "model_used": "Qwen3.8-27B",
        "quality_passed": True,
        "operational_outcome": "completed",
        "content_verdict": "usable",
        "cumulative_attempt_cost": 0.0,
        "final_attempt_cost": 0.0,
        "prompt_tokens": 319,
        "completion_tokens": 154,
        "cost_tier_name": "local_qwen",
        "timestamp": "2026-10-01T12:23:59.100000+00:00",
    }


@pytest.mark.unit
class TestDelegationRunAttributionFold:
    def test_keeps_counterfactual_session_and_actor_the_incoming_row_lacks(
        self,
    ) -> None:
        cf = _counterfactual()
        kept = HandlerDelegationRunAttributionFold().handle(
            ModelDelegationRunAttributionFoldRequest(
                stored={
                    "premium_counterfactual": cf,
                    "session_id": SESSION,
                    "delegated_by": "delegate-skill-orchestrator",
                },
                incoming={
                    "premium_counterfactual": None,
                    "session_id": None,
                    "delegated_by": "",
                },
            )
        )
        assert kept.row_columns() == {
            "premium_counterfactual": cf,
            "session_id": SESSION,
            "delegated_by": "delegate-skill-orchestrator",
        }

    def test_keeps_counterfactual_never_overrides_an_incoming_value(self) -> None:
        incoming_cf = _counterfactual()
        kept = HandlerDelegationRunAttributionFold().handle(
            ModelDelegationRunAttributionFoldRequest(
                stored={
                    "premium_counterfactual": {"counterfactual_cost_usd": "9"},
                    "session_id": "a-stored-session",
                    "delegated_by": "stored-actor",
                },
                incoming={
                    "premium_counterfactual": incoming_cf,
                    "session_id": SESSION,
                    "delegated_by": "delegate-skill-orchestrator",
                },
            )
        )
        assert kept.row_columns() == {}


@pytest.mark.unit
def test_sync_writer_keeps_counterfactual_when_canonical_lands_second() -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    handler.handle({**_delegate_skill_payload(), "_db": db})
    handler.handle({**_canonical_payload(), "_db": db})

    rows = db.query("delegation_events", {"correlation_id": RUN})
    assert len(rows) == 1
    row = rows[0]
    assert row["premium_counterfactual"] == _counterfactual()
    assert row["session_id"] == SESSION
    assert row["delegated_by"] == "delegate-skill-orchestrator"
    assert float(str(row["cost_savings_usd"])) > 0


@pytest.mark.unit
async def test_async_writer_keeps_counterfactual_when_canonical_lands_second() -> None:
    cf = _counterfactual()
    stored: dict[str, Any] = {
        "correlation_id": RUN,
        "premium_counterfactual": cf,
        "session_id": SESSION,
        "delegated_by": "delegate-skill-orchestrator",
        "cost_savings_usd": cf["counterfactual_cost_usd"],
    }
    db = MagicMock(spec=AsyncpgAdapter)
    db.execute = AsyncMock(return_value=[stored])
    runner = DelegationProjectionRunner()
    runner._db = db  # noqa: SLF001 — the runner's pool, replaced by the double

    row: dict[str, object] = {
        "correlation_id": RUN,
        "premium_counterfactual": None,
        "session_id": None,
        "delegated_by": None,
        "cost_savings_usd": 0.0,
    }
    await runner._preserve_existing_evidence_async(row)  # noqa: SLF001 — the step under test

    assert row["premium_counterfactual"] == cf
    assert row["session_id"] == SESSION
    assert row["delegated_by"] == "delegate-skill-orchestrator"
