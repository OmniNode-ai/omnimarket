# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_redeploy.

Publishes a rebuild command to the deploy agent via Kafka and waits for the
completion event. Requires KAFKA_BOOTSTRAP_SERVERS to be set.

Usage:
    python -m omnimarket.nodes.node_redeploy --scope full
    python -m omnimarket.nodes.node_redeploy --scope runtime --git-ref origin/main
    python -m omnimarket.nodes.node_redeploy --scope full --dry-run
    python -m omnimarket.nodes.node_redeploy --scope full --services svc1,svc2

Outputs JSON result to stdout. Exits 0 on success, 1 on failure/timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import uuid4

from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    ModelRedeployResult,
)

_log = logging.getLogger(__name__)


async def _run_dry(args: argparse.Namespace) -> int:
    from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
        ModelDeployRebuildCommand,
    )

    services = (
        [s.strip() for s in args.services.split(",") if s.strip()]
        if args.services
        else []
    )
    command = ModelDeployRebuildCommand(
        correlation_id=str(uuid4()),
        requested_by="node_redeploy-cli",
        scope=args.scope,
        services=services,
        git_ref=args.git_ref,
    )
    sys.stdout.write(
        json.dumps(
            {"dry_run": True, "would_publish": command.model_dump(mode="json")},
            indent=2,
        )
        + "\n"
    )
    return 0


async def _run_kafka(args: argparse.Namespace) -> int:
    from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
        HandlerRedeployKafka,
    )

    try:
        handler = HandlerRedeployKafka.from_env()
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.stderr.write(
            "Set KAFKA_BOOTSTRAP_SERVERS=<host:port> to enable Kafka-backed redeploy.\n"
        )
        return 1

    services = (
        [s.strip() for s in args.services.split(",") if s.strip()]
        if args.services
        else []
    )

    handler.timeout_s = args.timeout
    bus = handler.bus
    await bus.start()
    try:
        result: ModelRedeployResult = await handler.execute(
            scope=args.scope,
            git_ref=args.git_ref,
            services=services,
            requested_by="node_redeploy-cli",
        )
    finally:
        await bus.close()

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.success else 1


async def _run(args: argparse.Namespace) -> int:
    if args.dry_run:
        return await _run_dry(args)
    return await _run_kafka(args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Trigger a deploy agent rebuild via Kafka. "
            "Publishes a rebuild command and polls for the completion event."
        )
    )
    parser.add_argument(
        "--scope",
        choices=["full", "runtime", "core"],
        default="full",
        help="Rebuild scope (default: full)",
    )
    parser.add_argument(
        "--git-ref",
        default="origin/main",
        help="Git ref for deploy agent to pull (default: origin/main)",
    )
    parser.add_argument(
        "--services",
        default="",
        help="Comma-separated service filter. Empty = scope default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print command payload without publishing to Kafka.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for completion event (default: 600)",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
