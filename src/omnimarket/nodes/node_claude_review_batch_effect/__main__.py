# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one batch request over the in-memory bus and print every event read back (OMN-20910).

    python -m omnimarket.nodes.node_claude_review_batch_effect \
        --request FILE [--ceiling-tokens N]

``FILE`` is a ``ModelClaudeReviewBatchRequest`` as JSON. Admission is the stub
until the budget reducer lands. ``serve_and_read_back`` takes any lab-run bus, so a
lane bus drives the same chain. Prints one JSON line per event, in the order the
bus delivered them: the command, then each item's admission, cost and verdict,
then the terminal event. Exit 0 when the terminal event was read back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_claude_review_batch_effect.handlers.adapter_delegation_call_transport import (
    DelegationCallTransport,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.adapter_lane_bus import (
    BusEventSink,
    serve_and_read_back,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.handler_claude_review_batch import (
    HandlerClaudeReviewBatch,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.stub_review_admission import (
    StubReviewAdmission,
)
from omnimarket.nodes.node_claude_review_batch_effect.models import (
    ModelClaudeReviewBatchRequest,
)


async def _run(args: argparse.Namespace) -> int:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        HandlerLlmDelegationCall,
    )

    request = ModelClaudeReviewBatchRequest.model_validate_json(
        args.request.read_text(encoding="utf-8")
    )
    caller = HandlerLlmDelegationCall()

    def build(sink: BusEventSink) -> HandlerClaudeReviewBatch:
        return HandlerClaudeReviewBatch(
            transport=DelegationCallTransport(caller),
            admission=StubReviewAdmission(ceiling_tokens=args.ceiling_tokens),
            sink=sink,
        )

    bus = EventBusInmemory(environment="local", group="claude-review-batch")
    await bus.start()
    try:
        events = await serve_and_read_back(bus, request, build)
    finally:
        await bus.close()
    for event in events:
        sys.stdout.write(json.dumps(event) + "\n")
    return 0 if any(e["topic"].endswith("batch-completed.v1") for e in events) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--ceiling-tokens", type=int, default=200_000)
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
