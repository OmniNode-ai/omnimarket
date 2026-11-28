# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The composed-runtime boot verdict: a boot's observation and the declared bounds in, green or red out.

Pure. Red when any single callback held the event loop over the loop-hold bound (the finding names the call the stack
sampler saw), or when no /health probe was answered within the health bound after the container start. Both bounds come
on the request; none has a default or a literal here.
"""

from __future__ import annotations

from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_check_request import (
    ModelBootCheckRequest,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_check_result import (
    EnumBootFinding,
    ModelBootCheckResult,
    ModelBootFinding,
)


class HandlerBootCheck:
    """Judge one composed-runtime boot against the declared bounds."""

    def handle(self, request: ModelBootCheckRequest) -> ModelBootCheckResult:
        bounds = request.bounds
        findings: list[ModelBootFinding] = []

        holds = sorted(
            request.observation.loop_holds,
            key=lambda hold: hold.held_seconds,
            reverse=True,
        )
        for hold in holds:
            if hold.held_seconds > bounds.loop_hold_bound_seconds:
                findings.append(
                    ModelBootFinding(
                        kind=EnumBootFinding.LOOP_HOLD_OVER_BOUND,
                        detail=(
                            f"{hold.call} held the event loop {hold.held_seconds:.2f} s, "
                            f"over the {bounds.loop_hold_bound_seconds:g} s bound"
                        ),
                        call=hold.call,
                        seconds=hold.held_seconds,
                    )
                )

        answered = [s for s in request.observation.health_samples if s.answered]
        first_answer = (
            min(s.offset_seconds + s.latency_seconds for s in answered)
            if answered
            else None
        )
        if first_answer is None or first_answer > bounds.health_bound_seconds:
            findings.append(
                ModelBootFinding(
                    kind=EnumBootFinding.HEALTH_NOT_ANSWERED_WITHIN_BOUND,
                    detail=(
                        "no /health answer"
                        if first_answer is None
                        else f"first /health answer at {first_answer:.2f} s"
                    )
                    + f", bound {bounds.health_bound_seconds:g} s",
                    seconds=bounds.health_bound_seconds
                    if first_answer is None
                    else first_answer,
                )
            )

        return ModelBootCheckResult(
            green=not findings,
            findings=tuple(findings),
            first_answer_seconds=first_answer,
            longest_hold_seconds=holds[0].held_seconds if holds else 0.0,
        )
