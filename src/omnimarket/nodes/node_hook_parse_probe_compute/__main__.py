# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_hook_parse_probe_compute``: one JSON request on stdin, one JSON script on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_hook_parse_probe_compute.handlers import (
    HandlerHookParseProbe,
)
from omnimarket.nodes.node_hook_parse_probe_compute.models import (
    ModelHookParseProbeRequest,
)


def main() -> int:
    request = ModelHookParseProbeRequest.model_validate_json(sys.stdin.read() or "{}")
    sys.stdout.write(HandlerHookParseProbe().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
