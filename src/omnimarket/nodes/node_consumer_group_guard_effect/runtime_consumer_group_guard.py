# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator and tick entry point: print the census, exit 1 when the Empty-group count is over the contract threshold.

uv run python -m omnimarket.nodes.node_consumer_group_guard_effect --bus-lane dev

The host installer runs it as a registry module tick, which passes ``--omni-home`` and
``--state-dir`` and names the bus lane in ``ONEX_CONSUMER_GROUP_GUARD_BUS_LANE``. With a
state directory, every run writes ``census.json`` and only a run within the threshold
writes ``last-run.json``: the liveness watch reads that heartbeat, so a bus over the
threshold, or a census that could not be read, stops it and raises the alarm.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from omnimarket.models.consumer_group_guard import (
    ModelConsumerGroupCensus,
    ModelConsumerGroupGuardRequest,
)
from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_guard import (
    HandlerConsumerGroupGuardEffect,
)

BUS_LANE_VARIABLE = "ONEX_CONSUMER_GROUP_GUARD_BUS_LANE"


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def record(census: ModelConsumerGroupCensus, state_dir: Path) -> None:
    """Write the census, and the heartbeat only when the bus is within the threshold."""
    state_dir.mkdir(parents=True, exist_ok=True)
    text = census.model_dump_json(indent=2) + "\n"
    _write(state_dir / "census.json", text)
    if census.status == "OK":
        _write(state_dir / "last-run.json", text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus-lane", default=os.environ.get(BUS_LANE_VARIABLE))
    # Passed by the registry module tick; the census needs no registry path.
    parser.add_argument("--omni-home", default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.bus_lane:
        parser.error(f"--bus-lane or {BUS_LANE_VARIABLE} is required")
    census = asyncio.run(
        HandlerConsumerGroupGuardEffect().handle(
            ModelConsumerGroupGuardRequest(bus_lane=args.bus_lane)
        )
    )
    if args.state_dir is not None:
        record(census, args.state_dir)
    sys.stdout.write(census.model_dump_json(indent=2) + "\n")
    return 0 if census.status == "OK" else 1


__all__ = ["BUS_LANE_VARIABLE", "main", "record"]
