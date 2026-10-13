"""CLI entry point for node_session_phase_evaluator."""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID, uuid4

from omnimarket.nodes.node_session_phase_evaluator.handlers.handler_session_phase_evaluator import (
    HandlerSessionPhaseEvaluator,
    ModelPhaseEvaluationRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate session phase exit conditions and budget status."
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--correlation-id",
        default=None,
        help="UUID shared by every event of one tick (default: a fresh UUID)",
    )
    parser.add_argument("--phase-name", required=True)
    parser.add_argument("--max-duration-minutes", type=int, required=True)
    parser.add_argument("--elapsed-minutes", type=float, required=True)
    parser.add_argument(
        "--exit-condition-statuses",
        default="{}",
        help="JSON object mapping condition name to bool",
    )
    parser.add_argument("--halt-threshold-pct", type=int, default=100)
    args = parser.parse_args(argv)

    exit_condition_statuses: dict[str, bool] = json.loads(args.exit_condition_statuses)
    request = ModelPhaseEvaluationRequest(
        session_id=args.session_id,
        correlation_id=UUID(args.correlation_id) if args.correlation_id else uuid4(),
        phase_name=args.phase_name,
        max_duration_minutes=args.max_duration_minutes,
        elapsed_minutes=args.elapsed_minutes,
        exit_condition_statuses=exit_condition_statuses,
        halt_threshold_pct=args.halt_threshold_pct,
    )
    result = HandlerSessionPhaseEvaluator().handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
