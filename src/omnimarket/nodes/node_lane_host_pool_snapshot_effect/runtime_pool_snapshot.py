# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pass of the lane host pool snapshot step on its real backend (OMN-23664).

    python -m omnimarket.nodes.node_lane_host_pool_snapshot_effect

Reads the overlay (the file ONEX_POOL_SNAPSHOT_OVERLAY_FILE pins; otherwise
<root>/node_lane_host_pool_snapshot_effect/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS),
builds the ports from the overlay's routes, and runs one pass: read the four sources,
project the per-host rows, and, when the request writes, store the snapshot and record
and publish the record on the record topic. Each pass that writes a snapshot emits one
chain (start, then success or failure) to the floor's emission journal under the
request's correlation id.

Exit 0: written, or observed (write False). Exit 1: the pass ended in the typed failed
event. Exit 3: the overlay could not be read; nothing was written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.handler_pool_snapshot import (
    HandlerPoolSnapshot,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_sources import (
    resolve_path,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_wiring import (
    PoolSnapshotPorts,
    build_pool_snapshot_ports,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    EnumPoolSnapshotStatus,
    ModelPoolSnapshotRequest,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    ModelPoolSnapshotOverlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.protocols.protocol_pool_snapshot import (
    ProtocolPoolSnapshotPublisher,
)

NODE = "node_lane_host_pool_snapshot_effect"
PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
OBSERVE_FLAG = "--observe"


def run_once(
    overlay: ModelPoolSnapshotOverlay,
    *,
    ports: PoolSnapshotPorts | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    write: bool = True,
    publisher: ProtocolPoolSnapshotPublisher | None = None,
) -> tuple[int, dict[str, object]]:
    """One pass: the exit code and the line it prints."""
    if ports is None:
        ports = build_pool_snapshot_ports(
            overlay,
            environ=os.environ,
            publisher=publisher,
        )
    request = ModelPoolSnapshotRequest(correlation_id=uuid4(), write=write)
    chain: ChainEmitter | None = None
    if write and overlay.emission_journal is not None:
        chain = ChainEmitter(
            JsonlEmissionSink(resolve_path(overlay.emission_journal)),
            node=NODE,
            piece=overlay.floor_piece,
            correlation_id=request.correlation_id.hex,
        )
        chain.start()
    handler = HandlerPoolSnapshot(
        overlay=overlay,
        advertisement_source=ports.advertisements,
        host_table_source=ports.host_table,
        lanes_source=ports.lanes,
        accounts_source=ports.accounts,
        store=ports.store,
        publisher=ports.publisher,
        clock=now,
    )
    result = handler.handle(request)
    line: dict[str, object] = {
        "status": result.status.value,
        "correlation_id": str(request.correlation_id),
        "hosts": len(result.record.hosts) if result.record else 0,
        "free_slots_total": result.record.free_slots_total if result.record else 0,
    }
    if result.status is EnumPoolSnapshotStatus.WRITTEN:
        if chain is not None:
            chain.success(
                {
                    "hosts": int(str(line["hosts"])),
                    "free_slots_total": int(str(line["free_slots_total"])),
                }
            )
        return 0, line
    if result.status is EnumPoolSnapshotStatus.OBSERVED:
        line["read_at"] = result.record.read_at.isoformat() if result.record else ""
        line["rows"] = [
            row.model_dump(mode="json")
            for row in (result.record.hosts if result.record else ())
        ]
        return 0, line
    reason = result.failure_reason.value if result.failure_reason else "failed"
    if chain is not None:
        chain.failure(reason, result.failure_detail)
    line.update(failure_reason=reason, detail=result.failure_detail)
    return 1, line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_lane_host_pool_snapshot_effect.runtime_pool_snapshot",
        description="One pass of the lane host pool snapshot step: read the sources, project the rows, write and publish the snapshot",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay, reading the bus or writing a file",
    )
    parser.add_argument(
        OBSERVE_FLAG,
        action="store_true",
        help="Compute and print the per-host rows, write and publish nothing, still no chain",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """--probe exits 0 after one status line, before loading the overlay or running the pass."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return 0
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelPoolSnapshotOverlay, file_env=OVERLAY_FILE_ENV
        )
        code, line = run_once(overlay, write=not args.observe)
    except (NodeOverlayError, ValueError) as exc:
        code, line = 3, {"status": "overlay_refused", "detail": str(exc)}
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__: list[str] = ["main", "run_once"]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
