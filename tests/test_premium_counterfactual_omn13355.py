# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13355: auditable premium counterfactual carry path.

Proves the pinned premium counterfactual {model, price, as_of, tokens, cost} is
built from the canonical pricing manifest, carried onto the delegate-skill
metrics + the durable task-delegated event, and persisted (as JSONB) into the
delegation_events projection row through both the compat ``project()`` path and
the live ``project_delegate_skill_terminal()`` path. The saving stays auditable:
savings == counterfactual_cost_usd - actual cost, recomputable from the row.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.pricing import DEFAULT_BASELINE_MODEL, build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDelegation()


@pytest.mark.unit
class TestBuildPremiumCounterfactual:
    def test_pinned_from_manifest_with_provenance(self) -> None:
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        # Provenance fields are non-null and pinned to the manifest baseline.
        assert cf.model == DEFAULT_BASELINE_MODEL
        assert cf.price_in_per_1k == Decimal("0.015")
        assert cf.price_out_per_1k == Decimal("0.075")
        assert cf.as_of == "2026-02-01"
        assert cf.tokens_in == 1000
        assert cf.tokens_out == 500
        assert cf.pricing_source == "pricing_manifest"
        assert cf.measured is False

    def test_cost_recomputable_from_pins(self) -> None:
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        recomputed = (
            cf.price_in_per_1k * Decimal(cf.tokens_in)
            + cf.price_out_per_1k * Decimal(cf.tokens_out)
        ) / Decimal("1000")
        assert recomputed == cf.counterfactual_cost_usd
        assert cf.counterfactual_cost_usd == Decimal("0.0525")

    def test_unknown_premium_model_yields_none(self) -> None:
        cf = build_premium_counterfactual(
            prompt_tokens=10,
            completion_tokens=10,
            premium_model="model-not-in-manifest-xyz",
        )
        assert cf is None


@pytest.mark.unit
class TestProjectionPersistsCounterfactual:
    def test_compat_project_persists_jsonb(self) -> None:
        db = InmemoryDatabaseAdapter()
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-cf-001",
            task_type="code-review",
            delegated_to="local-qwen",
            quality_gate_passed=True,
            cost_usd=0.0,
            cost_savings_usd=0.0525,
            premium_counterfactual=cf,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("delegation_events", {"correlation_id": "corr-cf-001"})
        assert len(rows) == 1
        stored = rows[0]["premium_counterfactual"]
        assert stored is not None
        # Round-trips back to the typed model with full provenance.
        roundtrip = ModelPremiumCounterfactual.model_validate(stored)
        assert roundtrip.model == DEFAULT_BASELINE_MODEL
        assert roundtrip.as_of == "2026-02-01"
        # Saving is auditable: counterfactual - actual == recorded saving.
        saving = roundtrip.counterfactual_cost_usd - Decimal(str(rows[0]["cost_usd"]))
        assert saving == Decimal(str(rows[0]["cost_savings_usd"]))

    def test_terminal_projection_carries_counterfactual(self) -> None:
        db = InmemoryDatabaseAdapter()
        cf = build_premium_counterfactual(prompt_tokens=11, completion_tokens=22)
        assert cf is not None
        terminal = ModelDelegateSkillTerminalProjection.from_payload(
            {
                "status": "completed",
                "correlation_id": "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81",
                "task_type": "code_generation",
                "provider": "local-qwen",
                "model_name": "test-model-local",
                "response": "evidence proof",
                "quality_gate_passed": True,
                "quality_gates_failed": [],
                "metrics": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "total_tokens": 33,
                    "latency_ms": 42,
                    "cost_usd": 0.0,
                    "cost_savings_usd": float(cf.counterfactual_cost_usd),
                    "premium_counterfactual": cf.model_dump(mode="json"),
                },
            }
        )
        assert terminal.metrics.premium_counterfactual is not None
        result = HANDLER.project_delegate_skill_terminal(terminal, db)
        assert result.rows_upserted == 1
        rows = db.query(
            "delegation_events",
            {"correlation_id": "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"},
        )
        assert len(rows) == 1
        stored = rows[0]["premium_counterfactual"]
        assert stored is not None
        roundtrip = ModelPremiumCounterfactual.model_validate(stored)
        assert roundtrip.tokens_in == 11
        assert roundtrip.tokens_out == 22
        assert roundtrip.model == DEFAULT_BASELINE_MODEL

    def test_migration_declares_jsonb_column(self) -> None:
        from pathlib import Path

        migration = Path(
            "src/omnimarket/nodes/node_projection_delegation/migrations/"
            "0017_premium_counterfactual.sql"
        ).read_text()
        assert "ADD COLUMN IF NOT EXISTS premium_counterfactual JSONB" in migration
