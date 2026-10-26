# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-local CLI for agent-beacon Codex metadata capture (OMN-19513)."""

import argparse
import asyncio
import json
import socket
import sys
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.adapter_io import (
    FileBeaconLogReader,
    KafkaHookEventPublisher,
    ProjectionReadback,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.handler_beacon_trace_capture import (
    HandlerBeaconTraceCapture,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_trace_capture import (
    ModelBeaconTraceCaptureCommand,
)


async def _readback(event_ids: tuple[UUID, ...]) -> list[dict[str, Any]]:
    """Poll every five seconds, with a hard sixty-second deadline."""
    reader = ProjectionReadback()
    rows: list[dict[str, Any]] = []
    expected = set(event_ids)
    if not expected:
        return rows
    try:
        async with asyncio.timeout(60):
            while True:
                rows = await reader.read(event_ids)
                if {row["event_id"] for row in rows} >= expected:
                    return rows
                await asyncio.sleep(5)
    except TimeoutError:
        return rows


def main(argv: list[str] | None = None) -> int:
    """Publish a complete-line batch and print its checkpoint as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--lane")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--readback", action="store_true")
    parser.add_argument(
        "--bus-lane",
        help="Publish to this bus lane's declared broker with the host's identity.",
    )
    args = parser.parse_args(argv)
    try:
        command = ModelBeaconTraceCaptureCommand(
            log_path=args.log_path,
            offset=args.offset,
            lane=args.lane,
            host=args.host,
            dry_run=args.dry_run,
        )
    except ValidationError:
        parser.error("log-path and host must be non-blank; offset must be non-negative")

    publisher = KafkaHookEventPublisher(bus_lane=args.bus_lane)
    handler = HandlerBeaconTraceCapture(
        reader=FileBeaconLogReader(), publisher=publisher
    )
    try:
        result = handler.handle(command)
        publisher.flush()
        sys.stdout.write(result.model_dump_json() + "\n")
        if args.readback:
            rows = asyncio.run(_readback(result.event_ids))
            sys.stdout.write(json.dumps({"readback": rows}, default=str) + "\n")
    except Exception as exc:
        # Driver errors may embed connection credentials or trace content, so
        # only the exception's type is reported.
        sys.stderr.write(f"Beacon capture or readback failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
