# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_run_engine_effect < request.json``: run one operation, print the result.

A request is a ``ModelRunEngineRequest`` as JSON. The backend is the one the node overlay selects; the in-memory
backend keeps nothing between invocations, so a one-shot command is useful only against the durable backend.
"""

import asyncio
import sys

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_run_engine_effect import NodeRunEngineEffect
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    ModelRunEngineRequest,
)


def main() -> int:
    request = ModelRunEngineRequest.model_validate_json(sys.stdin.read())
    try:
        node = NodeRunEngineEffect()
    except NodeOverlayError as refused:
        sys.stderr.write(f"refused: {refused}\n")
        return 1
    result = asyncio.run(node.handle(request))
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
