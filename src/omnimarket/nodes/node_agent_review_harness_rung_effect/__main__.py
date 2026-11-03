# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the harness rung for one vote request read as JSON on stdin.

    echo '{"identity": "...", "model": "...", "rung": {...}, "prompt": "..."}' | \
        python -m omnimarket.nodes.node_agent_review_harness_rung_effect

Prints the vote as JSON on stdout and exits 0, or names the refusal on stderr and exits 1.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from .adapters.adapter_harness_runner import HarnessNotBoundError
from .handlers.handler_harness_rung import (
    HandlerAgentReviewHarnessRung,
    HarnessRungRefusedError,
)
from .models import ModelHarnessVoteRequest


def main() -> int:
    try:
        request = ModelHarnessVoteRequest.model_validate_json(sys.stdin.read())
        vote = asyncio.run(HandlerAgentReviewHarnessRung().handle(request))
    except (ValidationError, HarnessRungRefusedError, HarnessNotBoundError) as exc:
        sys.stderr.write(f"harness rung: no vote: {exc}\n")
        return 1
    sys.stdout.write(vote.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
