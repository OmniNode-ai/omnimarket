# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dispatch_queue_drainer."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from omnimarket.nodes.node_dispatch_queue_drainer.handlers import (
    HandlerDispatchQueueDrainer,
)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Compile one legacy dispatch queue YAML item without spawning."
    )
    parser.add_argument(
        "--queue-item-path",
        type=Path,
        default=None,
        help="Specific .onex_state/dispatch_queue/*.yaml file to compile.",
    )
    parser.add_argument(
        "--queue-dir",
        type=Path,
        default=None,
        help="Queue directory to scan when --queue-item-path is omitted.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Maximum queue items to process. First slice supports only 1.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="State directory for dispatch records and drainer result artifacts.",
    )
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help="Override TaskList directory for dispatch-worker dedup/fences.",
    )
    parser.add_argument(
        "--omni-home",
        type=Path,
        default=None,
        help="Override OMNI_HOME repo root used for missing-repo checks.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compatibility flag for skill dispatch. The drainer is already "
            "compile-only and never spawns agents or moves queue files."
        ),
    )

    args = parser.parse_args()
    result = HandlerDispatchQueueDrainer().handle(
        queue_item_path=args.queue_item_path,
        queue_dir=args.queue_dir,
        limit=args.limit,
        state_dir=args.state_dir,
        tasks_dir=args.tasks_dir,
        omni_home=args.omni_home,
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
