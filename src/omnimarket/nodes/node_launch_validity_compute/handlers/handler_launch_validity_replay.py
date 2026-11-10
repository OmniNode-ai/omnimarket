# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay recorded launches through the check and count the launches it would have refused."""

from __future__ import annotations

from collections import Counter

from ..models.model_launch_validity import (
    EnumRefusalCode,
    ModelLaunchValidityPolicy,
    ModelLaunchValidityReplayRequest,
    ModelLaunchValidityRequest,
    ModelReplayResult,
)
from .handler_launch_validity import HandlerLaunchValidity


class HandlerLaunchValidityReplay:
    def __init__(self, handler: HandlerLaunchValidity | None = None) -> None:
        self._handler = handler if handler is not None else HandlerLaunchValidity()

    def handle(self, request: ModelLaunchValidityReplayRequest) -> ModelReplayResult:
        total = 0
        refused: list[str] = []
        codes: Counter[EnumRefusalCode] = Counter()
        policy = request.policy
        for launch in request.launches:
            total += 1
            result = self._handler.handle(
                ModelLaunchValidityRequest(
                    target=launch.target, reads=launch.reads, policy=policy
                )
            )
            if result.refusals:
                refused.append(result.lane)
                codes.update({r.code for r in result.refusals})
        return ModelReplayResult(
            launches=total,
            would_refuse=len(refused),
            by_code=dict(codes),
            lanes=tuple(refused),
        )

    @staticmethod
    def count_line(
        result: ModelReplayResult, window_days: int, policy: ModelLaunchValidityPolicy
    ) -> str:
        by_code = " ".join(f"{c.value}={n}" for c, n in sorted(result.by_code.items()))
        return (
            f"LAUNCH-VALIDITY-REPLAY window_days={window_days} mode={policy.mode.value} "
            f"launches={result.launches} would_refuse={result.would_refuse}"
            + (f" {by_code}" if by_code else "")
        )
