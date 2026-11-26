# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line: ``<dry-run|install|status|readback> --host H [--overlay F] [--hosts F]``."""

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.nodes.node_host_observability_install_effect.models.model_observability import (
    ModelObservabilityInstallRequest,
)
from omnimarket.nodes.node_host_observability_install_effect.node import (
    NodeHostObservabilityInstallEffect,
)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="node_host_observability_install_effect")
    parser.add_argument("mode", choices=("dry-run", "install", "status", "readback"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--overlay", default="")
    parser.add_argument("--hosts", default="")
    args = parser.parse_args(list(argv))
    try:
        request = ModelObservabilityInstallRequest(
            host_name=args.host,
            mode=args.mode.replace("-", "_"),
            overlay_file=args.overlay,
            hosts_file=args.hosts,
        )
        result = NodeHostObservabilityInstallEffect().handle(request)
    except (ValidationError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.uncovered_hosts:
        sys.stdout.write(
            f"UNCOVERED lab_hosts rows with no declared target: {', '.join(result.uncovered_hosts)}\n"
        )
    return result.exit_code
