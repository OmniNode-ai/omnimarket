# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_session_phase_reducer.

Related:
    - OMN-11279: Create node_session_phase_reducer (REDUCER) in omnimarket
    - OMN-11224: Session phase control loop epic
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Session phase state reducer — apply an event to the current phase state."
    )
    parser.add_argument(
        "--event-type",
        required=True,
        help="Event type: session.started | session.ended | session.phase.state",
    )
    parser.add_argument("--session-id", required=True, help="Session identifier.")
    parser.add_argument(
        "--phase", default=None, help="Phase name (for session.phase.state events)."
    )
    parser.add_argument(
        "--phase-index", type=int, default=None, help="Phase index (0-based)."
    )
    parser.add_argument(
        "--budget-elapsed-pct",
        type=int,
        default=None,
        help="Budget elapsed percentage (0-100).",
    )
    parser.add_argument(
        "--active-worker-count",
        type=int,
        default=None,
        help="Number of active workers.",
    )
    parser.add_argument(
        "--state-path",
        default=".onex_state/session/phase_state.yaml",
        help="Path to write phase_state.yaml.",
    )
    parser.add_argument(
        "--state-json",
        default=None,
        help="Current state as JSON (omit to treat as initial event).",
    )
    args = parser.parse_args(argv)

    event: dict[str, object] = {
        "event_type": args.event_type,
        "session_id": args.session_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if args.phase is not None:
        event["phase"] = args.phase
    if args.phase_index is not None:
        event["phase_index"] = args.phase_index
    if args.budget_elapsed_pct is not None:
        event["budget_elapsed_pct"] = args.budget_elapsed_pct
    if args.active_worker_count is not None:
        event["active_worker_count"] = args.active_worker_count

    input_data: dict[str, object] = {"event": event}
    if args.state_json:
        input_data["state"] = json.loads(args.state_json)

    result = HandlerSessionPhaseReducer().handle(
        input_data=input_data,
        state_path=args.state_path,
    )
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
