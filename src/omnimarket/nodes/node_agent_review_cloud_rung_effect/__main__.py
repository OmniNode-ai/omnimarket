# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the cloud rung for one vote request read as JSON on stdin.

    echo '{"identity": "...", "model": "...", "repo": "o/r", "pr": 1, "head_sha": "..."}' | \
        python -m omnimarket.nodes.node_agent_review_cloud_rung_effect

Prints the unsigned vote as JSON on stdout and exits 0, or names the refusal on stderr and
exits 1. The overlay names the bus; the runner and its state come from the environment.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .adapters import CloudRungNotBoundError
from .handlers.handler_cloud_rung import (
    CloudRungRefusedError,
    HandlerAgentReviewCloudRung,
)
from .models import ModelCloudVoteRequest


def main() -> int:
    try:
        request = ModelCloudVoteRequest.model_validate_json(sys.stdin.read())
        vote = asyncio.run(HandlerAgentReviewCloudRung().handle(request))
    except (
        ValidationError,
        CloudRungRefusedError,
        CloudRungNotBoundError,
        NodeOverlayError,
    ) as exc:
        sys.stderr.write(f"cloud rung: no vote: {exc}\n")
        return 1
    sys.stdout.write(vote.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
