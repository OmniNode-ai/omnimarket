# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13408: a FAILED/escalation metered terminal must persist cost_usd > 0.

Live root cause (CID 21077717, dev lane 2026-06-24): a FAILED, escalation-
triggered, metered ``cheap_cloud`` (glm-5.2) terminal carried real served tokens
(input=103, output=1777) but persisted ``cost_usd=0.0`` in ``delegation_events``,
while ``savings_estimates`` quoted ``cloud_cost_usd=0.13482`` for the same session.
Tokens carried on the failed/escalation path; the measured cost did NOT.

The defect lived in the projection's ``_measure_actual_cost``: the failure path
emits the terminal with ``premium_counterfactual=None`` (no saving banked), and
the projection then trusted ``event.cost_usd`` verbatim instead of re-pricing the
served tokens through the serving tier's typed cost model. A terminal whose
``cost_usd`` was 0.0 stayed 0.0 even though the metered tier really served 1880
tokens.

Fix: the terminal cost is the source of truth, re-measured from the served usage
(the SAME ``recompute_actual_cost_and_savings`` the completed/savings path uses).
Whenever a serving tier is known and tokens were served, ``cost_usd`` is the
measured metered cost — regardless of whether a counterfactual saving is bankable.
There is no second estimate path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
    validate_actual_cost_provenance,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDelegation()


@pytest.mark.unit
class TestFailedMeteredTerminalCostUsd:
    def test_failed_escalation_metered_terminal_persists_measured_cost(self) -> None:
        """The live defect, reproduced: a FAILED metered terminal with served
        tokens and NO premium counterfactual must persist cost_usd > 0 — the
        measured metered cost — not the terminal's hardcoded/zeroed cost_usd."""
        db = InmemoryDatabaseAdapter()
        # Mirrors the live row: cheap_cloud (metered, 0.002/1k), glm-5.2, FAILED
        # quality gate, escalation_count=1, served 103/1777 tokens, but the
        # durable terminal carried cost_usd=0.0 and no counterfactual (failure
        # path banks no saving).
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-failed-metered-13408",
            task_type="reasoning",
            delegated_to="https://api.z.ai/api/coding/paas/v4/chat/completions",
            model_name="glm-5.2",
            quality_gate_passed=False,
            cost_usd=0.0,
            cost_savings_usd=0.0,
            tokens_input=103,
            tokens_output=1777,
            cost_tier_name="cheap_cloud",
            escalation_count=1,
            premium_counterfactual=None,
        )
        assert HANDLER.project(event, db).rows_upserted == 1
        row = db.query(
            "delegation_events", {"correlation_id": "corr-failed-metered-13408"}
        )[0]
        # 1880 tokens @ 0.002/1k = 0.00376 measured metered cost.
        assert Decimal(str(row["cost_usd"])) == Decimal("0.00376")
        assert row["cost_measurement_source"] == "metered"
        assert row["cost_tier_type"] == "metered"
        assert row["cost_tier_name"] == "cheap_cloud"
        # No counterfactual baseline on the failure path → no saving is quoted.
        assert Decimal(str(row["cost_savings_usd"])) == Decimal("0")
        validate_actual_cost_provenance(row)

    def test_failed_free_local_terminal_stays_zero_with_provenance(self) -> None:
        """A FAILED terminal on a free_local tier is honestly 0 cost — but the
        zero is PROVEN by the tier cost model (free_local), not a silent passthrough."""
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-failed-local-13408",
            task_type="reasoning",
            delegated_to="local-qwen",
            model_name="Qwen3.6-35B-A3B",
            quality_gate_passed=False,
            cost_usd=0.0,
            cost_savings_usd=0.0,
            tokens_input=120,
            tokens_output=900,
            cost_tier_name="local",
            premium_counterfactual=None,
        )
        assert HANDLER.project(event, db).rows_upserted == 1
        row = db.query(
            "delegation_events", {"correlation_id": "corr-failed-local-13408"}
        )[0]
        assert Decimal(str(row["cost_usd"])) == Decimal("0")
        assert row["cost_measurement_source"] == "free_local"
        validate_actual_cost_provenance(row)

    def test_failed_terminal_authoritative_nonzero_cost_is_trusted_verbatim(
        self,
    ) -> None:
        """When the FAILED terminal already carries a non-zero cost_usd, it is the
        AUTHORITATIVE total (final + prior, summed once by _emit_terminal). The
        projection must NOT re-add escalation_history — that would double-count the
        terminal tier, whose own entry is in that history. (OMN-13535 invariant.)"""
        db = InmemoryDatabaseAdapter()
        # _emit_terminal computed cost_usd = final(0.00376) + prior(0.0012) = 0.00496,
        # and the terminal tier's own attempt is also present in escalation_history.
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-failed-authoritative-13408",
            task_type="reasoning",
            delegated_to="cloud-glm",
            model_name="glm-5.2",
            quality_gate_passed=False,
            cost_usd=0.00496,
            cost_savings_usd=0.0,
            tokens_input=103,
            tokens_output=1777,
            cost_tier_name="cheap_cloud",
            escalation_count=1,
            escalation_history=[
                {
                    "tier_name": "cheap_cloud",
                    "model_used": "glm-5.2",
                    "cost_usd": 0.0012,
                    "prompt_tokens": 60,
                    "completion_tokens": 540,
                },
                {
                    "tier_name": "cheap_cloud",
                    "model_used": "glm-5.2",
                    "cost_usd": 0.00376,
                    "prompt_tokens": 103,
                    "completion_tokens": 1777,
                },
            ],
            premium_counterfactual=None,
        )
        assert HANDLER.project(event, db).rows_upserted == 1
        row = db.query(
            "delegation_events", {"correlation_id": "corr-failed-authoritative-13408"}
        )[0]
        # Trusted verbatim — NOT 0.00496 + escalation_history(0.00496) = double count.
        assert Decimal(str(row["cost_usd"])) == Decimal("0.00496")
        assert row["cost_measurement_source"] == "metered"
        validate_actual_cost_provenance(row)
