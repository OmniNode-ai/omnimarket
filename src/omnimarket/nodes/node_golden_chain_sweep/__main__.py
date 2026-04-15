# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_golden_chain_sweep.

Validates end-to-end Kafka-to-DB-projection golden chains.
Chain definitions are loaded from golden_chains.yaml at startup.
Pre-collected projection data is passed via --projected-rows JSON.

Usage:
    python -m omnimarket.nodes.node_golden_chain_sweep \
        --chains registration,routing \
        --timeout-ms 15000

Outputs JSON to stdout: GoldenChainSweepResult model.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from pydantic import ValidationError

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    GoldenChainSweepRequest,
    NodeGoldenChainSweep,
)
from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    # Load chain definitions from YAML registry (falls back to [] if file missing)
    _all_chains = load_registry()
    _chain_map = {c.name: c for c in _all_chains}

    parser = argparse.ArgumentParser(
        description="Validate golden chains from Kafka topics to DB projections."
    )
    parser.add_argument(
        "--chains",
        default="",
        help="Comma-separated chain names to validate (default: all registry chains)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15000,
        help="Validation timeout per chain in milliseconds (default: 15000)",
    )
    parser.add_argument(
        "--projected-rows",
        default="{}",
        help=(
            "JSON object mapping chain name to projected row dict "
            "(default: empty — all chains will show timeout)"
        ),
    )

    args = parser.parse_args()

    chain_filter = [c.strip() for c in args.chains.split(",") if c.strip()]
    chains = (
        [_chain_map[n] for n in chain_filter if n in _chain_map]
        if chain_filter
        else _all_chains
    )

    try:
        projected_rows: dict[str, dict[str, object]] = json.loads(args.projected_rows)
    except json.JSONDecodeError as exc:
        _log.error("invalid --projected-rows JSON: %s", exc)
        sys.exit(1)

    try:
        request = GoldenChainSweepRequest(
            chains=chains,
            timeout_ms=args.timeout_ms,
            projected_rows=projected_rows,
        )
    except ValidationError as exc:
        _log.error("invalid --projected-rows content: %s", exc)
        sys.exit(1)

    handler = NodeGoldenChainSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("pass",):
        sys.exit(1)


if __name__ == "__main__":
    main()
