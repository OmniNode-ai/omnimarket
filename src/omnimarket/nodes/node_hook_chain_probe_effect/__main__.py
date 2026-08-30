# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the hook->cloud chain probe (OMN-17202 AC4).

    uv run python -m omnimarket.nodes.node_hook_chain_probe_effect

Runs the probe from the operator Mac with NO cluster access and prints the
typed per-leg result as JSON. Exit code 0 when the chain completes end to end,
1 when it does not -- so a goal row can cite this process's exit status as its
terminal read instead of a per-leg proof that cannot answer the question.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from omnimarket.nodes.node_hook_chain_probe_effect.handlers.handler_hook_chain_probe import (
    HandlerHookChainProbe,
)
from omnimarket.nodes.node_hook_chain_probe_effect.models.model_hook_chain_probe import (
    ModelHookChainProbeRequest,
)


async def _run(request: ModelHookChainProbeRequest) -> int:
    result = await HandlerHookChainProbe().handle(request)
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2) + "\n")
    return 0 if result.chain_complete else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="node_hook_chain_probe_effect",
        description="Trace one correlated hook event across all five legs.",
    )
    parser.add_argument(
        "--correlation-id",
        default=None,
        help="Correlation id to trace; minted when omitted.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per-leg observation deadline.",
    )
    args = parser.parse_args(argv)
    request = ModelHookChainProbeRequest(
        correlation_id=args.correlation_id,
        timeout_seconds=args.timeout_seconds,
    )
    return asyncio.run(_run(request))


if __name__ == "__main__":
    sys.exit(main())
