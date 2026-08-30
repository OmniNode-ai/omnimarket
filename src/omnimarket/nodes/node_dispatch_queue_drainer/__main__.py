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
from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerRequest,
)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Compile the next QUEUED dispatch-queue item and durably advance "
            "its lifecycle, without spawning agents or moving queue files."
        )
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
            "Select, validate and compile the next QUEUED item but mutate "
            "nothing: no lifecycle transition, no dispatch record, no result "
            "artifact. Reports status 'dry_run'."
        ),
    )
    parser.add_argument(
        "--claim-lease-seconds",
        type=int,
        default=900,
        help=(
            "Renewable claim lease. Expiry marks the claim stale for observers; "
            "it never deletes the queue item."
        ),
    )
    parser.add_argument(
        "--dispatch-ack-timeout-seconds",
        type=int,
        default=900,
        help=(
            "How long a dispatched item may go unacknowledged before it is "
            "observably pending."
        ),
    )
    parser.add_argument(
        "--actor",
        type=str,
        default="node_dispatch_queue_drainer",
        help="Actor recorded on every lifecycle transition this run writes.",
    )

    args = parser.parse_args()
    payload = ModelDispatchQueueDrainerRequest(
        queue_item_path=args.queue_item_path,
        queue_dir=args.queue_dir,
        limit=args.limit,
        state_dir=args.state_dir,
        tasks_dir=args.tasks_dir,
        omni_home=args.omni_home,
        dry_run=args.dry_run,
        claim_lease_seconds=args.claim_lease_seconds,
        dispatch_ack_timeout_seconds=args.dispatch_ack_timeout_seconds,
        actor=args.actor,
    )
    result = HandlerDispatchQueueDrainer().handle(payload)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
