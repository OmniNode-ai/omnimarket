# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installer adapter: preserve declared install-only nodes before swapping src."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .models import ModelInstallOnlyRequest
from .node import NodeInstallOnlyPreserveEffect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--stage-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = NodeInstallOnlyPreserveEffect().handle(
            ModelInstallOnlyRequest(
                live_root=args.install_root, stage_root=args.stage_root
            )
        )
    except (NodeOverlayError, OSError, ValueError) as exc:
        sys.stderr.write(f"INSTALL-ONLY-PRESERVE-REFUSED: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
