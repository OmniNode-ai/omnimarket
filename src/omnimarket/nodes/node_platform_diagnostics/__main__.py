# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point for `uv run onex run node_platform_diagnostics`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from omnimarket.nodes.node_platform_diagnostics.handlers.handler_platform_diagnostics import (
    HandlerPlatformDiagnostics,
    ModelDiagnosticsRequest,
)
from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
    EnumDiagnosticDimension,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="node_platform_diagnostics")
    parser.add_argument(
        "--dimensions",
        type=str,
        default="",
        help="Comma-separated list of dimensions to check (empty = all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no external side effects)",
    )
    parser.add_argument(
        "--freshness-threshold-hours",
        type=int,
        default=4,
        help="Freshness threshold in hours for cached artifacts",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()

    dimensions: list[EnumDiagnosticDimension] = []
    if args.dimensions:
        for dim_str in args.dimensions.split(","):
            dim_str = dim_str.strip().upper()
            try:
                dimensions.append(EnumDiagnosticDimension(dim_str))
            except ValueError:
                print(f"Unknown dimension: {dim_str}", file=sys.stderr)  # noqa: T201
                sys.exit(1)

    request = ModelDiagnosticsRequest(
        dimensions=dimensions,
        dry_run=args.dry_run,
        freshness_threshold_hours=args.freshness_threshold_hours,
    )

    handler = HandlerPlatformDiagnostics()
    result = await handler.handle(request)
    print(json.dumps(result.model_dump(mode="json"), indent=2))  # noqa: T201


if __name__ == "__main__":
    asyncio.run(_main())
