# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure scoring of supplied paired recordings, without model calls or rendering."""

from __future__ import annotations

from decimal import Decimal, localcontext

from omnimarket.nodes.node_context_gate_cascade_compute.models.model_gate_cascade import (
    ModelGateCascadeCaseResult,
    ModelGateCascadeRequest,
    ModelGateCascadeResult,
)


class HandlerContextGateCascade:
    """Require exact per-case recall parity and positive measured net savings."""

    def handle(self, request: ModelGateCascadeRequest) -> ModelGateCascadeResult:
        rows: list[ModelGateCascadeCaseResult] = []
        # Accommodate all supplied finite decimal places and the sum's carry,
        # independent of the caller's ambient Decimal context.
        costs = [
            cost
            for row in request.observations
            for cost in (
                row.ungated_cost_usd,
                row.gate_cost_usd,
                row.downstream_cost_usd,
            )
        ]
        precision = (
            max(cost.adjusted() for cost in costs)
            - min(int(cost.as_tuple().exponent) for cost in costs)
            + len(str(len(costs)))
            + 3
        )
        with localcontext() as context:
            context.prec = max(28, precision)
            for row in sorted(
                request.observations, key=lambda row: (row.case_id, row.repeat)
            ):
                expected = len(row.expected_label_ids)
                ungated_hits = len(row.ungated_recalled_ids)
                cascade_hits = len(row.cascade_recalled_ids)
                cascade_cost = row.gate_cost_usd + row.downstream_cost_usd
                rows.append(
                    ModelGateCascadeCaseResult(
                        case_id=row.case_id,
                        repeat=row.repeat,
                        expected_count=expected,
                        ungated_recalled_count=ungated_hits,
                        cascade_recalled_count=cascade_hits,
                        ungated_recall=ungated_hits / expected if expected else None,
                        cascade_recall=cascade_hits / expected if expected else None,
                        # Compare integers, never rounded aggregate/float recall.
                        recall_equal=ungated_hits == cascade_hits,
                        gate_admitted=row.gate_admitted,
                        ungated_cost_usd=row.ungated_cost_usd,
                        cascade_cost_usd=cascade_cost,
                        cost_saved_usd=row.ungated_cost_usd - cascade_cost,
                    )
                )
            ungated_cost = sum((row.ungated_cost_usd for row in rows), Decimal(0))
            cascade_cost = sum((row.cascade_cost_usd for row in rows), Decimal(0))
            saved = ungated_cost - cascade_cost
        parity = all(row.recall_equal for row in rows)
        return ModelGateCascadeResult(
            profile_id=request.profile_id,
            role=request.role,
            gate=request.gate,
            winner_rung_id=request.winner_rung_id,
            winner_evidence_id=request.winner_evidence_id,
            label_set_id=request.label_set_id,
            gate_training_evidence_id=request.gate_training_evidence_id,
            observations=tuple(rows),
            recall_equal_everywhere=parity,
            ungated_cost_usd=ungated_cost,
            cascade_cost_usd=cascade_cost,
            cost_saved_usd=saved,
            recommended=parity and saved > 0,
        )
