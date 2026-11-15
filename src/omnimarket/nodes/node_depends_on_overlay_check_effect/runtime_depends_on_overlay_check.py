# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_depends_on_overlay_check_effect``: refuse a node whose declared overlay row is absent (OMN-24537).

Exit codes: 0 every declared dependency is present; 1 a declared dependency is absent (or malformed, or of an unknown kind);
3 a deployment fact is missing (INDETERMINATE): a root that cannot be read.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.nodes.node_depends_on_overlay_check_effect.handlers.handler_depends_on_overlay_check import (
    HandlerDependsOnOverlayCheck,
)
from omnimarket.nodes.node_depends_on_overlay_check_effect.models import (
    ModelDependsOnOverlayCheckCommand,
)

NODE_NAME = "node_depends_on_overlay_check_effect"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("--nodes-root", required=True)
    parser.add_argument("--overlays-root", required=True)
    parser.add_argument("--contract-file", required=True)
    parser.add_argument("--overlay-file", required=True)
    parser.add_argument("--overlay-kind", required=True)
    parser.add_argument("--node", action="append", default=[], dest="nodes")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        result = HandlerDependsOnOverlayCheck().handle(
            ModelDependsOnOverlayCheckCommand(
                nodes_root=args.nodes_root,
                overlays_root=args.overlays_root,
                contract_file_name=args.contract_file,
                overlay_file_name=args.overlay_file,
                overlay_kind=args.overlay_kind,
                node_names=tuple(args.nodes),
            )
        )
    except (OSError, ValidationError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    for finding in result.findings:
        sys.stdout.write(f"refused {finding.node} {finding.code}: {finding.detail}\n")
    sys.stdout.write(
        f"depends-on-overlay: nodes={result.nodes_checked} dependencies={result.dependencies_checked} "
        f"overlays={result.overlays_present} refused={len(result.findings)}\n"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
