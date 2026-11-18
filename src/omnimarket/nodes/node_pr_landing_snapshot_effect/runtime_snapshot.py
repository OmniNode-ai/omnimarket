# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pass of the landing snapshot step on its real backend (OMN-23409): the unit's tick.

    python -m omnimarket.nodes.node_pr_landing_snapshot_effect

Reads the overlay (the file ONEX_LANDING_SNAPSHOT_OVERLAY_FILE pins, for a second instance such as
the cutover repositories' unit; otherwise <root>/node_pr_landing_snapshot_effect/overlay.yaml under
ONEX_SKILL_OVERLAY_ROOTS), takes the tick the controller's facts file holds now, and, when that
tick is not recorded yet, fills and records it: appended to the record journal and published on
the record topic. A tick already recorded is left alone and nothing is published. Each pass that
takes a snapshot writes one chain (start, then success or failure) to the floor's emission
journal under the request's correlation id.

Exit 0: recorded, or the tick was already recorded. Exit 1: the tick ended in the typed failed
event. Exit 3: the overlay or the facts file could not be read; nothing was recorded.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.journal_retention import (
    retention_line,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    build_controller_source,
    build_snapshot_ports,
    resolve_path,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.protocols.protocol_landing_snapshot import (
    ProtocolLandingSnapshotPublisher,
)

NODE = "node_pr_landing_snapshot_effect"
PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"


def run_once(
    overlay: ModelSnapshotOverlay,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    publisher: ProtocolLandingSnapshotPublisher | None = None,
) -> tuple[int, dict[str, object]]:
    """One pass: the exit code and the line it prints."""
    facts, write, recorder, publisher_ = build_snapshot_ports(
        overlay, now=now, publisher=publisher
    )
    controller = build_controller_source(overlay, now=now)
    try:
        tick = facts.current_tick()
    except SnapshotSourceError as exc:
        return 3, {"status": "facts_unread", "detail": str(exc)}
    if any(r.tick == tick for r in recorder.recorded()):
        return 0, {
            "status": "already_recorded",
            "tick": tick,
            **retention_line(recorder.decision, trimmed=False),
        }
    request = ModelLandingSnapshotRequest(correlation_id=uuid4(), tick=tick)
    if (
        overlay.emission_journal is None
    ):  # the overlay's validator refuses this for redpanda
        raise ValueError("the redpanda backend needs emission_journal")
    chain = ChainEmitter(
        JsonlEmissionSink(resolve_path(overlay.emission_journal)),
        node=NODE,
        piece=overlay.floor_piece,
        correlation_id=request.correlation_id.hex,
        origin="real",
    )
    chain.start()
    result = HandlerPrLandingSnapshot(
        overlay=overlay,
        facts_source=facts,
        write_source=write,
        recorder=recorder,
        publisher=publisher_,
        controller_source=controller,
    ).handle(request)
    line: dict[str, object] = {
        "status": result.status.value,
        "tick": tick,
        "correlation_id": str(request.correlation_id),
        "prs": len(result.record.facts.prs) if result.record else 0,
        "node_ids_filled": result.node_ids_filled,
        "run_ids_filled": result.run_ids_filled,
        "controller_tick": (
            result.record.controller.controller_tick
            if result.record and result.record.controller
            else None
        ),
        **retention_line(recorder.decision, trimmed=recorder.trimmed),
    }
    if result.status is EnumSnapshotStatus.RECORDED:
        chain.success(
            {
                "prs": int(str(line["prs"])),
                "node_ids_filled": result.node_ids_filled,
                "run_ids_filled": result.run_ids_filled,
            }
        )
        return 0, line
    reason = result.failure_reason.value if result.failure_reason else "failed"
    chain.failure(reason, result.failure_detail)
    line.update(failure_reason=reason, detail=result.failure_detail)
    return 1, line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot",
        description="One pass of the landing snapshot step: fill and record the tick the controller's facts file holds now",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay, reading the bus or writing a file",
    )
    add_tick_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """--probe exits 0 after one status line; a pass accepts --omni-home and --state-dir and writes last-run.json, the unit heartbeat."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return 0
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelSnapshotOverlay, file_env=OVERLAY_FILE_ENV
        )
        code, line = run_once(overlay)
    except (NodeOverlayError, ValueError) as exc:
        code, line = 3, {"status": "overlay_refused", "detail": str(exc)}
    write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__: list[str] = ["main", "run_once"]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
