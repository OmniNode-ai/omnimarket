# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 5 (OMN-12845 / M5): regression-lock the on_vs_off fixture_mode guard.

M5 delivers the live closed loop through the dedicated runner +
feedback-edge reducer path — NOT by retrofitting the pure on_vs_off COMPUTE
scorer into a live I/O node. This regression-lock asserts the on_vs_off handler
still refuses ``fixture_mode=False`` (returns a typed ``runtime_mode_not_implemented``
failure rather than fabricating output), documenting that the live path lives in
the M5 runner/feedback edge and the offline scorer stays pure.
"""

from __future__ import annotations

from omnimarket.nodes.node_on_vs_off_experiment_compute.handlers.handler_on_vs_off_experiment import (
    HandlerOnVsOffExperiment,
)
from omnimarket.nodes.node_on_vs_off_experiment_compute.models.model_on_vs_off_request import (
    ModelOnVsOffPricing,
    ModelOnVsOffRequest,
    ModelOnVsOffTask,
)


def _request(*, fixture_mode: bool) -> ModelOnVsOffRequest:
    return ModelOnVsOffRequest(
        run_id="run-regression",
        model_id="local-coder",
        tasks=(
            ModelOnVsOffTask(
                task_id="task_001",
                description="emit a hello function",
                on_prompt_tokens=100,
                on_completion_tokens=50,
                off_prompt_tokens=80,
                off_completion_tokens=40,
            ),
        ),
        pricing=ModelOnVsOffPricing(
            prompt_cost_per_1k=0.001,
            completion_cost_per_1k=0.002,
        ),
        fixture_mode=fixture_mode,
    )


class TestNoLivePathToday:
    def test_runtime_mode_still_refused(self) -> None:
        """fixture_mode=False is still a typed failure, not a fabricated result."""
        result = HandlerOnVsOffExperiment().handle(_request(fixture_mode=False))
        assert result.status == "failed"
        assert result.failure_class == "runtime_mode_not_implemented"

    def test_fixture_mode_still_succeeds(self) -> None:
        """The replay-proven offline path stays green."""
        result = HandlerOnVsOffExperiment().handle(_request(fixture_mode=True))
        assert result.status == "ok"
