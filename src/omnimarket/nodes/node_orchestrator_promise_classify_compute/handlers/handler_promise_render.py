# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick projection: candidate counts are visible, candidate words are never printed."""

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseRenderRequest,
    ModelPromiseRenderResult,
)


class HandlerPromiseRender:
    def handle(self, request: ModelPromiseRenderRequest) -> ModelPromiseRenderResult:
        # Stable capture ids also prevent a duplicated receipt from inflating a tick's counts.
        captures = {
            capture.utterance.capture_id: capture for capture in request.captures
        }
        candidates = sum(
            item.state == "candidate"
            for capture in captures.values()
            for item in capture.classification.items
        )
        low = sum(
            capture.classification.low_confidence for capture in captures.values()
        )
        lines = [f"Promise candidates: {candidates}; low-confidence review: {low}"]
        for capture in sorted(
            captures.values(), key=lambda value: value.utterance.capture_id
        ):
            for item in capture.classification.items:
                if item.state == "candidate":
                    continue
                age = max(
                    0,
                    (request.now - capture.utterance.captured_at).total_seconds() / 60,
                )
                lines.append(
                    f"{item.item_id} owner={item.owner} state={item.state} age={age:.0f}m {item.quote}"
                )
        return ModelPromiseRenderResult(
            text="\n".join(lines), candidate_count=candidates, low_confidence_count=low
        )
