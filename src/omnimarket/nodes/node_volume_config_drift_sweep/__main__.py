# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_volume_config_drift_sweep (OMN-12958).

Usage:
    python -m omnimarket.nodes.node_volume_config_drift_sweep --dry-run
    python -m omnimarket.nodes.node_volume_config_drift_sweep \
        --sidecar /path/to/.config_provenance.json --lane stability
    python -m omnimarket.nodes.node_volume_config_drift_sweep \
        --deployed /path/to/volume/bifrost_delegation.yaml

Outputs JSON to stdout: VolumeConfigDriftSweepResult model. Exits non-zero when
drift is found so callers can gate on it.
"""

from __future__ import annotations

import logging
import sys

from omnimarket.nodes.node_volume_config_drift_sweep.handlers.handler_volume_config_drift_sweep import (
    NodeVolumeConfigDriftSweep,
    VolumeConfigDriftSweepRequest,
)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    import argparse

    parser = argparse.ArgumentParser(
        description="Volume-vs-packaged config drift detection for runtime-rendered configs."
    )
    parser.add_argument(
        "--config-name",
        default="bifrost_delegation",
        help="Logical config identifier (default: bifrost_delegation)",
    )
    parser.add_argument(
        "--lane",
        default="local",
        help="Runtime lane label for the deployed copy (default: local)",
    )
    parser.add_argument(
        "--sidecar",
        default=None,
        help="Path to a provenance sidecar JSON probed off a runtime lane",
    )
    parser.add_argument(
        "--deployed",
        default=None,
        help="Path to the deployed (volume) contract copy",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Path to packaged source (default: omnimarket package config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report findings only — no ticket creation",
    )

    args = parser.parse_args()

    request = VolumeConfigDriftSweepRequest(
        config_name=args.config_name,
        lane=args.lane,
        sidecar_path=args.sidecar,
        deployed_path=args.deployed,
        source_path=args.source,
        dry_run=args.dry_run,
    )

    handler = NodeVolumeConfigDriftSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status != "clean":
        sys.exit(1)


if __name__ == "__main__":
    main()
