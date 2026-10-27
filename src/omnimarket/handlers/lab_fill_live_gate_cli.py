# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the lab fill's live Linear gate.

The plugin's idle-slot fill runs ``python -m omnimarket.handlers.lab_fill_live_gate_cli``
with ``{"tickets": [...], "operator": ...}`` on stdin just before it sends lanes, and
sends only the tickets whose verdict says ``send``.
"""

from __future__ import annotations

import sys

from omnimarket.handlers.handler_lab_fill_live_tickets import (
    HandlerLabFillLiveGate,
    Post,
)
from omnimarket.models.lab_fill import ModelLabFillLiveGateRequest


def main(post: Post | None = None) -> int:
    """Read one request from stdin and write its live gate plan as one JSON line."""
    try:
        request = ModelLabFillLiveGateRequest.model_validate_json(sys.stdin.read())
    except (ValueError, OSError):
        sys.stderr.write("Invalid lab-fill live gate request\n")
        return 2
    plan = HandlerLabFillLiveGate(post=post).handle(request)
    sys.stdout.write(plan.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
