# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_typed_decision_effect.

Usage:
    python -m omnimarket.nodes.node_typed_decision_effect < request.json

Reads one ModelTypedDecisionRequest as JSON on stdin and writes the
ModelTypedDecisionResult as JSON on stdout. The result never carries the key:
it is resolved inside the handler from the secret store by the routing
contract's reference, and nothing on the command line or in the output names
its value. The incumbent is optional; without it the request is blind.
Exit 0 when the model decided, 3 when the incumbent answered or there is no answer.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    ModelTypedDecisionRequest,
)

_EXIT_NOT_MODEL = 3
_VISIBILITY_CACHE_FILE = "typed_decision_visibility_cache.json"


def _visibility_cache_path() -> Path | None:
    """Where one process leaves its visibility reads for the next, or None.

    Each CLI call is a fresh process, so an in-process cache would never be hit.
    The file lives in the workspace state directory. With no OMNI_HOME the cache
    is simply off (a miss is a live read, so this can only cost a read, never
    admit a repository).
    """
    home = os.environ.get("OMNI_HOME")
    if not home:
        return None
    return Path(home) / ".onex_state" / _VISIBILITY_CACHE_FILE


def main() -> int:
    request = ModelTypedDecisionRequest.model_validate_json(sys.stdin.read())
    result = HandlerTypedDecision(
        visibility_cache_path=_visibility_cache_path()
    ).handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.decided_by is EnumTypedDecisionDecider.MODEL else _EXIT_NOT_MODEL


if __name__ == "__main__":
    raise SystemExit(main())
