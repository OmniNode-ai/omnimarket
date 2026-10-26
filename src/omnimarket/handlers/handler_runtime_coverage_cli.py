# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read command-serving redundancy on an explicitly selected bus lane."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from omnimarket.handlers.handler_runtime_coverage_read import (
    read_health_runtime_coverage,
    read_runtime_coverage,
)
from omnimarket.models.runtime_coverage import (
    ModelRuntimeCoverageRequest,
    ModelRuntimeRegistryEndpoint,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus-lane", required=True)
    parser.add_argument("--runtime-lane", action="append", default=[])
    parser.add_argument("--contract-root", action="append", default=[])
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Read the shared health policy and canonical workspace contract roots",
    )
    parser.add_argument(
        "--registry-config",
        type=Path,
        help="YAML list of published dev runtime manifest endpoints",
    )
    parser.add_argument(
        "--coverage-config",
        type=Path,
        help="Path to deployment runtime coverage policy with --workspace",
    )
    args = parser.parse_args()
    if args.workspace:
        if args.contract_root or args.runtime_lane or args.registry_config:
            parser.error(
                "--workspace cannot be combined with explicit contract roots, runtime lanes or registries"
            )
        result = asyncio.run(
            read_health_runtime_coverage(
                args.workspace, args.bus_lane, args.coverage_config
            )
        )
        sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2) + "\n")
        return 0 if result.status == "OK" else 1
    if args.coverage_config:
        parser.error("--coverage-config requires --workspace")
    if not args.contract_root or not args.runtime_lane:
        parser.error("provide --workspace, or both --contract-root and --runtime-lane")
    registries = (
        ()
        if args.registry_config is None
        else tuple(
            ModelRuntimeRegistryEndpoint.model_validate(row)
            for row in yaml.safe_load(args.registry_config.read_text())
        )
    )
    result = asyncio.run(
        read_runtime_coverage(
            ModelRuntimeCoverageRequest(
                bus_lane=args.bus_lane,
                runtime_lanes=tuple(args.runtime_lane),
                contract_roots=tuple(args.contract_root),
                runtime_registries=registries,
            )
        )
    )
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2) + "\n")
    return 0 if result.status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
