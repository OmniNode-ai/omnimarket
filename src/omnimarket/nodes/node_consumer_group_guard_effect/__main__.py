# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_consumer_group_guard_effect --bus-lane dev``: the consumer-group guard command line."""

import sys

from omnimarket.nodes.node_consumer_group_guard_effect import (
    runtime_consumer_group_guard,
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "schedule":
        from .runtime_consumer_group_guard_schedule import main as schedule_main

        return schedule_main(arguments[1:])
    return runtime_consumer_group_guard.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
