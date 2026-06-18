# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_judge_verdict_parse_compute."""

from omnimarket.nodes.node_judge_verdict_parse_compute.handlers.handler_judge_verdict_parse_compute import (
    HandlerJudgeVerdictParseCompute,
)


def test_handler_judge_verdict_parse_compute_importable() -> None:
    assert HandlerJudgeVerdictParseCompute is not None
