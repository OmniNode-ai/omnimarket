# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13408: the canonical delegation-failed.v1 terminal projects metered cost.

Root cause (STRIKE THREE, live-proven on the .201 dev lane, CID 5120dd9c):
the emitter half of OMN-13408 is fixed — the compat ``task-delegated.v1`` event
carries the honest metered ``cost_usd`` (e.g. ``0.01924`` on a FAILED metered
cheap_cloud escalation). But the projection ALSO consumes the canonical
``onex.evt.omnibase-infra.delegation-failed.v1`` terminal (a ``ModelDelegationResult``)
and UPSERTs the SAME ``correlation_id`` row.

The canonical ``ModelDelegationResult`` carries the real metered spend in
``cumulative_attempt_cost`` / ``final_attempt_cost`` and in
``escalation_history[<winning-tier>].cost_usd`` — but has NO top-level
``cost_usd`` / ``cost_tier_name`` / ``cost_measurement_source`` field (those do
not exist on the canonical DTO by design). The projection's
``_canonical_result_to_task_delegated_payload`` converter dropped every cost
dimension, so the canonical event projected ``cost_usd=0.0`` with an empty serving
tier — flooring the row to 0 and clobbering the compat event's honest cost.

These tests drive the canonical-event projection path directly and assert the
metered ``cost_usd > 0`` survives into the projection row. They fail before the
converter is taught to carry the cost dimensions, and pass after.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelTaskDelegatedEvent,
    _canonical_result_to_task_delegated_payload,
    _measure_actual_cost,
)

# Live CID 5120dd9c-4205-4407-8342-f859469d3641 shape (dev lane, 2026-06-25):
# local (free) failed QG -> cheap_cloud glm-5.2 (metered, z.ai) failed QG -> FAILED.
_CID = "5120dd9c-4205-4407-8342-f859469d3641"
_METERED_COST_USD = 0.01924
_METERED_PROMPT_TOKENS = 88
_METERED_COMPLETION_TOKENS = 9532


def _canonical_failed_terminal_payload() -> dict[str, object]:
    """The canonical delegation-failed.v1 payload as the projection receives it.

    Mirrors ``ModelDelegationResult.model_dump(mode="json")`` for the live FAILED
    metered escalation terminal: NO top-level cost/tier/source field, the metered
    spend lives only in ``cumulative_attempt_cost`` / ``final_attempt_cost`` and in
    the winning ``escalation_history`` entry.
    """
    return {
        "correlation_id": _CID,
        "task_type": "reasoning",
        "model_used": "glm-5.2",
        "quality_passed": False,
        "failure_reason": "quality_gate_rejected",
        "latency_ms": 12_000,
        "prompt_tokens": _METERED_PROMPT_TOKENS,
        "completion_tokens": _METERED_COMPLETION_TOKENS,
        "total_tokens": _METERED_PROMPT_TOKENS + _METERED_COMPLETION_TOKENS,
        "escalation_count": 1,
        "cumulative_attempt_cost": _METERED_COST_USD,
        "final_attempt_cost": _METERED_COST_USD,
        "cumulative_input_tokens": _METERED_PROMPT_TOKENS,
        "cumulative_output_tokens": _METERED_COMPLETION_TOKENS,
        "escalation_history": (
            {
                "tier_name": "local",
                "model_used": "Qwen3.6-35B-A3B",
                "cost_usd": 0.0,
                "prompt_tokens": 96,
                "completion_tokens": 3399,
            },
            {
                "tier_name": "cheap_cloud",
                "model_used": "glm-5.2",
                "cost_usd": _METERED_COST_USD,
                "prompt_tokens": _METERED_PROMPT_TOKENS,
                "completion_tokens": _METERED_COMPLETION_TOKENS,
            },
        ),
    }


@pytest.mark.unit
class TestCanonicalFailedTerminalCostProjectionOmn13408:
    """The canonical FAILED metered terminal must not floor cost_usd to 0."""

    def test_converter_carries_metered_cost_dimensions(self) -> None:
        """The canonical→task-delegated converter carries the real metered cost.

        Before the fix the converter emitted neither ``cost_usd`` nor
        ``cost_tier_name``, so the projection floored the row to 0. ``cost_usd``
        carries the canonical ``cumulative_attempt_cost`` and ``cost_tier_name``
        resolves the winning metered escalation tier — the two load-bearing fields
        ``_measure_actual_cost`` needs to avoid the unknown-tier 0.0 fall-through.
        """
        payload = _canonical_failed_terminal_payload()
        converted = _canonical_result_to_task_delegated_payload(payload)

        assert converted.get("cost_usd") == pytest.approx(_METERED_COST_USD)
        assert converted.get("cost_tier_name") == "cheap_cloud"

    def test_projection_persists_metered_cost_on_canonical_failed_terminal(
        self,
    ) -> None:
        """End-to-end: the canonical FAILED terminal projects cost_usd>0 (the DoD).

        This is the STRIKE THREE live shape: the row materialized from the
        canonical ``delegation-failed.v1`` must carry the metered ``cost_usd`` the
        escalation_history winning tier recorded, not 0.0.
        """
        payload = _canonical_result_to_task_delegated_payload(
            _canonical_failed_terminal_payload()
        )
        event = ModelTaskDelegatedEvent(**payload)
        measurement = _measure_actual_cost(event)

        assert measurement.cost_usd == pytest.approx(_METERED_COST_USD, abs=1e-4)
        assert measurement.cost_usd > 0.0
        # FAILED path carries no auditable counterfactual baseline → savings stays 0
        # (never counterfactual-minus-0).
        assert measurement.cost_savings_usd == 0.0
        assert measurement.cost_tier_name == "cheap_cloud"
        assert measurement.cost_measurement_source == "metered"

    def test_free_only_canonical_terminal_stays_zero(self) -> None:
        """Regression guard: a free-local-only FAILED terminal honestly stays 0.

        No metered winner in escalation_history → no cost to hoist → cost_usd=0 by
        the tier cost model, with no spurious tier label.
        """
        payload = _canonical_failed_terminal_payload()
        payload["model_used"] = "Qwen3.6-35B-A3B"
        payload["cumulative_attempt_cost"] = 0.0
        payload["final_attempt_cost"] = 0.0
        payload["escalation_history"] = (
            {
                "tier_name": "local",
                "model_used": "Qwen3.6-35B-A3B",
                "cost_usd": 0.0,
                "prompt_tokens": 96,
                "completion_tokens": 3399,
            },
        )
        converted = _canonical_result_to_task_delegated_payload(payload)
        event = ModelTaskDelegatedEvent(**converted)
        measurement = _measure_actual_cost(event)

        assert measurement.cost_usd == 0.0
        assert measurement.cost_savings_usd == 0.0
