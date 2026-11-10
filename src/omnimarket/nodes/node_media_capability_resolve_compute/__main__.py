# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_media_capability_resolve_compute``: one JSON request on stdin, one JSON resolution on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_media_capability_resolve_compute.handlers import (
    HandlerMediaCapabilityResolve,
)
from omnimarket.nodes.node_media_capability_resolve_compute.models import (
    ModelMediaCapabilityRequest,
)


def main() -> int:
    request = ModelMediaCapabilityRequest.model_validate_json(sys.stdin.read() or "{}")
    sys.stdout.write(HandlerMediaCapabilityResolve().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
