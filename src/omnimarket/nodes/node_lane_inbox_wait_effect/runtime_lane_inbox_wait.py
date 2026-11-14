# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wires the wait handler to the overlay's backend, and one wait from the CLI (OMN-24459).

``python -m omnimarket.nodes.node_lane_inbox_wait_effect --lane LANE --seconds N [--since STAMP]``
prints one JSON line of the result. Exit 0: the budget elapsed with nothing binding (waited).
Exit 10: woken by binding rows. Exit 11: a stop order arrived. Exit 75: the inbox is unreadable.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.lane_inbox_wait import (
    EnumInboxWaitOutcome,
    ModelLaneInboxWaitCommand,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.adapters.inmemory_adapters import (
    FakeClock,
    FakeSleeper,
    InMemoryInboxSource,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.adapters.runtime_adapters import (
    MonotonicClock,
    ReaderInboxSource,
    TimeSleeper,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.handlers.handler_lane_inbox_wait import (
    HandlerLaneInboxWait,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.overlay import (
    ModelLaneInboxWaitOverlay,
    load_overlay,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"

EXIT = {
    EnumInboxWaitOutcome.WAITED: 0,
    EnumInboxWaitOutcome.WOKEN: 10,
    EnumInboxWaitOutcome.STOP: 11,
    EnumInboxWaitOutcome.UNREADABLE: 75,
}


def _inmemory(overlay: ModelLaneInboxWaitOverlay) -> HandlerLaneInboxWait:
    clock = FakeClock()
    return HandlerLaneInboxWait(
        overlay=overlay,
        source=InMemoryInboxSource(),
        sleeper=FakeSleeper(clock),
        clock=clock,
    )


def _runtime(overlay: ModelLaneInboxWaitOverlay) -> HandlerLaneInboxWait:
    return HandlerLaneInboxWait(
        overlay=overlay,
        source=ReaderInboxSource(overlay.reader, os.environ),
        sleeper=TimeSleeper(),
        clock=MonotonicClock(),
    )


_BACKEND_BUILDERS: dict[
    str, Callable[[ModelLaneInboxWaitOverlay], HandlerLaneInboxWait]
] = {"inmemory": _inmemory, "runtime": _runtime}


def build_handler(overlay: ModelLaneInboxWaitOverlay) -> HandlerLaneInboxWait:
    """The wait handler wired to the overlay's backend."""
    return _BACKEND_BUILDERS[overlay.backend](overlay)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    handler: HandlerLaneInboxWait | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_lane_inbox_wait_effect")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--seconds", required=True, type=float)
    parser.add_argument("--since")
    parser.add_argument("--correlation-id")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    started = datetime.now(UTC).strftime(_STAMP)
    command = ModelLaneInboxWaitCommand(
        correlation_id=args.correlation_id or f"lane-inbox-wait-{started}",
        lane=args.lane,
        seconds=args.seconds,
        # No since given: the CLI fills it with the stamp at start, so the wait counts
        # only rows that arrive after it began.
        since=args.since or started,
    )
    result = (handler if handler is not None else build_handler(overlay)).handle(
        command
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return EXIT[result.outcome]


__all__ = ["EXIT", "build_handler", "main"]
