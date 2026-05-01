# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_version_skew_detector.

Usage:
    python -m omnimarket.nodes.node_version_skew_detector \
        --plugin-version 1.2.3 \
        --runtime-version 1.2.0 \
        --nodes node_a:1.0.0,node_b:2.1.0

Outputs JSON to stdout: VersionSkewResult model.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from omnimarket.nodes.node_version_skew_detector.handlers.handler_version_skew_detector import (
    NodeVersionInfo,
    NodeVersionSkewDetector,
    VersionSkewCheckRequest,
)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Detect plugin/runtime version incompatibility."
    )
    parser.add_argument(
        "--plugin-version", required=True, help="Plugin version (semver)"
    )
    parser.add_argument(
        "--runtime-version", required=True, help="Runtime version (semver)"
    )
    parser.add_argument(
        "--nodes",
        default="",
        help="Comma-separated node:version pairs (e.g. node_a:1.0.0,node_b:2.1.0)",
    )
    parser.add_argument(
        "--runtime-compat-range",
        default=">=1.0.0,<2.0.0",
        help="Semver range for node compatibility (default: >=1.0.0,<2.0.0)",
    )

    args = parser.parse_args()

    installed_nodes: list[NodeVersionInfo] = []
    if args.nodes:
        for pair in args.nodes.split(","):
            pair = pair.strip()
            if ":" in pair:
                name, version = pair.rsplit(":", 1)
                installed_nodes.append(NodeVersionInfo(name=name, version=version))

    request = VersionSkewCheckRequest(
        plugin_version=args.plugin_version,
        runtime_version=args.runtime_version,
        installed_nodes=installed_nodes,
        runtime_compat_range=args.runtime_compat_range,
    )

    handler = NodeVersionSkewDetector()
    result = handler.handle(request)
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2) + "\n")

    if result.status == "skew_detected":
        sys.exit(1)


if __name__ == "__main__":
    main()
