# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the trajectory evaluation node (OMN-20087).

    uv run python -m omnimarket.nodes.node_trajectory_evaluation_effect (submit|poll) < command.json

Reads one JSON command from stdin, runs it through the handler with the lane
setting ONEX_TRAJECTORY_EVALUATION_BACKEND, and prints the outcome as JSON.
Exit 0 for Accepted and Status, 3 for Refused, 1 for Failed.
"""

from __future__ import annotations

import asyncio
import sys

from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
    HandlerTrajectoryEvaluation,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationFailed,
    ModelTrajectoryEvaluationPoll,
    ModelTrajectoryEvaluationRefused,
    ModelTrajectoryEvaluationSubmit,
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("submit", "poll"):
        sys.stderr.write(
            "usage: python -m omnimarket.nodes.node_trajectory_evaluation_effect (submit|poll) < command.json\n"
        )
        return 2
    raw = sys.stdin.read()
    command: ModelTrajectoryEvaluationSubmit | ModelTrajectoryEvaluationPoll = (
        ModelTrajectoryEvaluationSubmit.model_validate_json(raw)
        if args[0] == "submit"
        else ModelTrajectoryEvaluationPoll.model_validate_json(raw)
    )
    outcome = asyncio.run(HandlerTrajectoryEvaluation().handle(command))
    sys.stdout.write(outcome.model_dump_json() + "\n")
    if isinstance(outcome, ModelTrajectoryEvaluationRefused):
        return 3
    if isinstance(outcome, ModelTrajectoryEvaluationFailed):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
