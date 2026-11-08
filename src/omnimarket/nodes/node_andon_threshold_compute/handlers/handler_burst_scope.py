# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide, in one pass over one tick's observations, which pieces a friction burst stops."""

from __future__ import annotations

from omnimarket.models.model_andon_threshold import (
    ModelBurstScopeRequest,
    ModelBurstScopeResult,
    ModelPieceDecision,
    ModelThresholdBreach,
)


class HandlerBurstScope:
    """A piece stops when a class it originated exceeds its frozen threshold; no other piece does."""

    def handle(self, request: ModelBurstScopeRequest) -> ModelBurstScopeResult:
        thresholds = request.freeze.thresholds
        breaches: dict[int, list[ModelThresholdBreach]] = {
            piece: [] for piece in request.pieces
        }
        unmeasured: set[str] = set()
        for observation in request.observations:
            threshold = thresholds.get(observation.class_id)
            if threshold is None:
                unmeasured.add(observation.class_id)
            elif observation.count_24h > threshold:
                breaches[observation.piece].append(
                    ModelThresholdBreach(
                        class_id=observation.class_id,
                        count_24h=observation.count_24h,
                        threshold=threshold,
                    )
                )
        decisions = tuple(
            ModelPieceDecision(
                piece=piece,
                decision="stopped" if breaches[piece] else "admitted",
                breaches=tuple(breaches[piece]),
            )
            for piece in request.pieces
        )
        return ModelBurstScopeResult(
            decisions=decisions,
            stopped_pieces=tuple(d.piece for d in decisions if d.decision == "stopped"),
            admitted_pieces=tuple(
                d.piece for d in decisions if d.decision == "admitted"
            ),
            unmeasured_classes=tuple(sorted(unmeasured)),
        )
