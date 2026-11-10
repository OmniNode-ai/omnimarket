# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the container unit installer: ``<install|status|uninstall|dry-run> --host H --unit U``."""

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    ModelContainerUnitsRequest,
)
from omnimarket.nodes.node_host_installer_container_units.node import (
    NodeHostInstallerContainerUnits,
)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="node_host_installer_container_units")
    parser.add_argument("mode", choices=("install", "status", "uninstall", "dry-run"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--overlay", default="")
    args = parser.parse_args(list(argv))
    try:
        request = ModelContainerUnitsRequest(
            host_name=args.host,
            unit=args.unit,
            mode=args.mode.replace("-", "_"),
            overlay_file=args.overlay,
        )
        result = NodeHostInstallerContainerUnits().handle(request)
    except (NodeOverlayError, ValidationError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code
