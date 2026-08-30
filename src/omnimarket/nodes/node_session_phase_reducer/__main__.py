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
    ModelSessionPhaseReducerInput,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Session phase state reducer — fold ONE event and print the "
            "resulting phase state. Preview only: the durable state of record "
            "is a database row the runtime maintains on the bus path "
            "(OMN-16924), so this command reads and writes nothing."
        )
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
    args = parser.parse_args(argv)

    # OMN-16790: the handler's def-B input is the WIRE payload of one event, not
    # a {state, event} envelope.
    #
    # OMN-16924: `--state-path` is GONE, with no replacement. The reducer's
    # state of record is a row in the database, loaded and persisted by the
    # runtime's state_io dispatch seam around handle(); a CLI process is not the
    # runtime and does not touch the database. So this command folds ONE event
    # against NO prior state and prints the result — a preview of the delta, not
    # a state mutation. Durable folds happen on the bus.
    request = ModelSessionPhaseReducerInput(
        event_type=args.event_type,
        session_id=args.session_id,
        timestamp=datetime.now(UTC),
        phase=args.phase,
        phase_index=args.phase_index,
        budget_elapsed_pct=args.budget_elapsed_pct,
        active_worker_count=args.active_worker_count,
    )

    result = HandlerSessionPhaseReducer().handle(request)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
