# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_main_load_check_effect``.

Load every overlay document through the model class in this tree, or report what is missing.

Exit codes: 0 every overlay was loaded; 1 an overlay could not be loaded; 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    check_overlays,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_main_load_check import (
    HandlerOverlayMainLoadCheck,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.model_resolver import (
    default_search_roots,
    resolve_spec,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models import (
    ModelOverlayMainLoadCommand,
)

NODE_NAME = "node_overlay_main_load_check_effect"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("--overlays-root", required=True)
    parser.add_argument("--pins-file", required=True)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    handler = HandlerOverlayMainLoadCheck(
        resolve=lambda spec: resolve_spec(spec, default_search_roots()),
        floor_check=check_overlays,
    )
    try:
        result = handler.handle(
            ModelOverlayMainLoadCommand(
                overlays_root=args.overlays_root,
                pins_file=args.pins_file,
            )
        )
    except (OSError, ValidationError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    for node in result.loaded:
        sys.stdout.write(f"ok {node}\n")
    refused = 0
    noted = 0
    for finding in result.findings:
        if finding.refuses:
            refused += 1
            sys.stdout.write(
                f"refused {finding.node} {finding.code}: {finding.detail}\n"
            )
        else:
            noted += 1
            sys.stdout.write(f"note {finding.node} {finding.code}: {finding.detail}\n")
    sys.stdout.write(
        f"overlay-main-load: loaded={len(result.loaded)} refused={refused} noted={noted}\n"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
