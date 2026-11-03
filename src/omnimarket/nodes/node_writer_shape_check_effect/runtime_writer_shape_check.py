# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hook and CI entrypoint of the projection runner shape check (OMN-18883).

    python -m omnimarket.nodes.node_writer_shape_check_effect.runtime_writer_shape_check

Run from the repository root. Exit 0 only when no finding remains.
"""

import sys
from pathlib import Path

from omnimarket.nodes.node_writer_shape_check_effect.handlers.handler_projection_runner_shape_check import (
    HandlerProjectionRunnerShapeCheck,
)
from omnimarket.nodes.node_writer_shape_check_effect.handlers.projection_runner_frozen_set import (
    DEDICATED_DEPLOYMENT_RUNNERS,
    FROZEN_RUNNERS,
    MIN_RUNNERS,
)
from omnimarket.nodes.node_writer_shape_check_effect.models import (
    ModelProjectionRunnerShapeRequest,
)

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    root = Path.cwd()
    nodes_root = root / "src" / "omnimarket" / "nodes"
    if not nodes_root.is_dir():
        sys.stderr.write("ERROR: run from repo root (src/omnimarket/nodes not found)\n")
        return 2
    result = HandlerProjectionRunnerShapeCheck().handle(
        ModelProjectionRunnerShapeRequest(
            nodes_root=str(nodes_root),
            package_root=str(root / "src"),
            frozen_runners=FROZEN_RUNNERS,
            dedicated_deployments=DEDICATED_DEPLOYMENT_RUNNERS,
            min_runners=MIN_RUNNERS,
        )
    )
    for finding in result.findings:
        sys.stderr.write(f"{finding.rule}: {finding.subject}: {finding.message}\n")
    if result.findings:
        sys.stderr.write(
            f"projection-runner-shape: {len(result.findings)} refusal(s)\n"
        )
        return 1
    sys.stdout.write(
        f"projection-runner-shape: {result.runners_seen} runner subclasses, clean\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
