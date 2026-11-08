# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lane_result_sweep_effect <verb> --since <stamp>``.

``sweep``  classify the receipts since the stamp and append the class, action and reading rows.
``probe``  the same judgement, appending nothing.

Each verb prints the result as one JSON object. Exit codes: 0 the run completed (a NOT WORKING
reading is still 0: a reading refuses nothing); 1 the run failed (an append that did not land);
3 a deployment fact is missing (no overlay, no host name, no ledger route).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_lane_result_sweep import ModelLaneResultSweepCommand
from omnimarket.nodes.node_lane_result_sweep_effect.handlers.handler_lane_result_sweep import (
    HandlerLaneResultSweep,
)
from omnimarket.nodes.node_lane_result_sweep_effect.overlay import load_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)


def _run(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    since = datetime.fromisoformat(arguments.since.replace("Z", "+00:00")).astimezone(
        UTC
    )
    handler = HandlerLaneResultSweep(
        overlay=overlay,
        ledger=build_row_source(overlay.ledger_read),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    result = handler.handle(
        ModelLaneResultSweepCommand(
            correlation_id=arguments.correlation_id or str(uuid.uuid4()),
            since=since,
            write=write,
        )
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.status == "completed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_lane_result_sweep_effect")
    parser.add_argument("verb", choices=("sweep", "probe"))
    parser.add_argument("--since", required=True)
    parser.add_argument("--correlation-id")
    arguments = parser.parse_args(argv)
    try:
        return _run(arguments, write=arguments.verb == "sweep")
    except (NodeOverlayError, LedgerRouteError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        return 3


__all__ = ["main"]
