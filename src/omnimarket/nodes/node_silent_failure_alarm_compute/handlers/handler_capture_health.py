# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide classifier silence from distinct prompt observations, without guessing causes."""

from __future__ import annotations

from omnimarket.models.model_capture_health import (
    ModelCaptureHealthFinding,
    ModelCaptureHealthRequest,
    ModelCaptureHealthResult,
    ModelClassifierObservation,
)


class HandlerCaptureHealth:
    def handle(self, request: ModelCaptureHealthRequest) -> ModelCaptureHealthResult:
        policy = request.policy
        distinct = {o.capture_id: o for o in request.observations}
        causes: dict[str, int] = {}
        tail: list[ModelClassifierObservation] = []
        measurements = []
        for observation in distinct.values():
            if observation.call is not None:
                measurements.append(observation.call)
            if observation.classifier == policy.fallback_classifier:
                cause = observation.call.outcome if observation.call else "unmeasured"
                causes[cause] = causes.get(cause, 0) + 1
                tail.append(observation)
            else:
                tail.clear()
        findings: tuple[ModelCaptureHealthFinding, ...] = ()
        if len(tail) >= policy.unanswered_window:
            findings = (
                ModelCaptureHealthFinding(
                    alarm=policy.alarm,
                    episode=tail[0].capture_id,
                    unanswered=len(tail),
                    why=f"delegated classifier answered none of the last {policy.unanswered_window} prompts",
                    fix=policy.fix,
                ),
            )
        return ModelCaptureHealthResult(
            findings=findings, causes=causes, measurements=tuple(measurements)
        )
