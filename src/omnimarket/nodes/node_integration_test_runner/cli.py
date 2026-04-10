"""CLI entrypoint for node_integration_test_runner.

Usage:
    uv run python -m omnimarket.nodes.node_integration_test_runner.cli \\
        --feature node_create_ticket --profile local
    uv run python -m omnimarket.nodes.node_integration_test_runner.cli --all
    uv run python -m omnimarket.nodes.node_integration_test_runner.cli \\
        --feature node_ticket_pipeline --profile staging
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.nodes.node_integration_test_runner.handlers.handler_integration_test_runner import (
    HandlerIntegrationTestRunner,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_request import (
    EnumDIProfile,
    ModelIntegrationTestRunnerRequest,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run golden chain tests with swappable DI profiles."
    )
    parser.add_argument(
        "--feature",
        metavar="NODE_NAME",
        help="Run only this node's golden chain tests (e.g. node_create_ticket)",
    )
    parser.add_argument(
        "--all",
        dest="all_nodes",
        action="store_true",
        help="Run all discovered nodes' tests",
    )
    parser.add_argument(
        "--profile",
        choices=[p.value for p in EnumDIProfile],
        default=EnumDIProfile.LOCAL.value,
        help="DI profile: local (default) | staging | production",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover tests but do not execute them",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        metavar="SECONDS",
        help="Per-node timeout in seconds (default 120)",
    )
    args = parser.parse_args()

    request = ModelIntegrationTestRunnerRequest(
        profile=EnumDIProfile(args.profile),
        feature=args.feature,
        all_nodes=args.all_nodes,
        dry_run=args.dry_run,
        timeout_per_node_s=args.timeout,
    )

    handler = HandlerIntegrationTestRunner()
    result = handler.handle(request)

    print(json.dumps(result.model_dump(), indent=2, default=str))  # noqa: T201
    sys.exit(0 if result.overall_status in ("pass", "dry_run", "skipped") else 1)


if __name__ == "__main__":
    main()
