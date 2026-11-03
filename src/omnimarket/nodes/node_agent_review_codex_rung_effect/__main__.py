# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the codex rung for one vote request read as JSON on stdin.

    echo '{"identity": "...", "model": "...", "prompt": "..."}' | \
        python -m omnimarket.nodes.node_agent_review_codex_rung_effect

Prints the vote as JSON on stdout and exits 0, or names the refusal on stderr and exits 1.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from .adapters.adapter_harness_runner import HarnessNotBoundError
from .handlers.handler_codex_rung import (
    CodexRungRefusedError,
    HandlerAgentReviewCodexRung,
)
from .models import ModelCodexVoteRequest


def main() -> int:
    try:
        request = ModelCodexVoteRequest.model_validate_json(sys.stdin.read())
        vote = asyncio.run(HandlerAgentReviewCodexRung().handle(request))
    except (ValidationError, CodexRungRefusedError, HarnessNotBoundError) as exc:
        sys.stderr.write(f"codex rung: no vote: {exc}\n")
        return 1
    sys.stdout.write(vote.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
