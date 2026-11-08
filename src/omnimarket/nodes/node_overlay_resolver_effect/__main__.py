# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_resolver_effect < request.json``: resolve one overlay, print the result."""

import sys

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_overlay_resolver_effect import NodeOverlayResolverEffect
from omnimarket.nodes.node_overlay_resolver_effect.models.model_overlay_resolver import (
    ModelOverlayResolveRequest,
)


def main() -> int:
    request = ModelOverlayResolveRequest.model_validate_json(sys.stdin.read())
    try:
        result = NodeOverlayResolverEffect().handle(request)
    except NodeOverlayError as refused:
        sys.stderr.write(f"refused: {refused}\n")
        return 1
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
