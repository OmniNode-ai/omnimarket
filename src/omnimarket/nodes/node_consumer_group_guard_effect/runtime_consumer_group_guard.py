# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator and tick entry point: print the census, exit 1 when the Empty-group count is over the contract threshold.

uv run python -m omnimarket.nodes.node_consumer_group_guard_effect --bus-lane dev
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from omnimarket.models.consumer_group_guard import ModelConsumerGroupGuardRequest
from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_guard import (
    HandlerConsumerGroupGuardEffect,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus-lane", required=True)
    args = parser.parse_args(argv)
    census = asyncio.run(
        HandlerConsumerGroupGuardEffect().handle(
            ModelConsumerGroupGuardRequest(bus_lane=args.bus_lane)
        )
    )
    sys.stdout.write(census.model_dump_json(indent=2) + "\n")
    return 0 if census.status == "OK" else 1


__all__ = ["main"]
