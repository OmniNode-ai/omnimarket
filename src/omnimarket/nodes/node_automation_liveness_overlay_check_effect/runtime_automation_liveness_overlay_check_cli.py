# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the liveness overlay completeness check (OMN-20804).

``onex-automation-liveness-check`` reads the overlay, the sources declaration and every file of
every registry repository the declaration names, hands the text to the pure handler, and prints
what it found. Exit 0 means the overlay and the installers agree, 1 means findings, 2 means the
check could not run (a repository is absent and ``--internal-only`` was not asked for).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .handlers.handler_automation_liveness_overlay_check_run import (
    HandlerAutomationLivenessOverlayCheckRun,
)
from .models import ModelAutomationLivenessOverlayCheckRunRequest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onex-automation-liveness-check", description=__doc__
    )
    parser.add_argument(
        "--internal-root",
        type=Path,
        default=Path.cwd(),
        help="the private checkout holding the overlay (default: the current directory)",
    )
    parser.add_argument(
        "--registry-root",
        type=Path,
        default=None,
        help="the directory holding the other registry clones (default: $OMNI_HOME)",
    )
    parser.add_argument(
        "--private-repository",
        default=None,
        help="the name the sources declaration gives the private checkout "
        "(default: the directory name of --internal-root)",
    )
    parser.add_argument(
        "--internal-only",
        action="store_true",
        help="scan only this checkout; the result says it is partial",
    )
    parser.add_argument(
        "--overlay", type=Path, default=None, help="overlay file to check"
    )
    parser.add_argument(
        "--sources", type=Path, default=None, help="sources declaration to check"
    )
    args = parser.parse_args(argv)

    registry_value = args.registry_root or os.environ.get("OMNI_HOME")
    result = HandlerAutomationLivenessOverlayCheckRun().handle(
        ModelAutomationLivenessOverlayCheckRunRequest(
            internal_root=args.internal_root,
            registry_root=Path(registry_value) if registry_value else None,
            private_repository=args.private_repository,
            internal_only=args.internal_only,
            overlay=args.overlay,
            sources=args.sources,
        )
    )
    for line in result.stderr_lines:
        sys.stderr.write(f"{line}\n")
    for line in result.stdout_lines:
        sys.stdout.write(f"{line}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
