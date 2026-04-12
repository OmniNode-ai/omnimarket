# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_golden_chain_sweep.

Validates end-to-end Kafka-to-DB-projection golden chains.
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
    ModelChainDefinition,
    NodeGoldenChainSweep,
)
from omnimarket.nodes.node_golden_chain_sweep.topics import (
    LLM_ROUTING_DECISION_TOPIC,
    PATTERN_STORED_TOPIC,
    ROUTING_DECISION_TOPIC,
    RUN_EVALUATED_TOPIC,
    TASK_DELEGATED_TOPIC,
)

_log = logging.getLogger(__name__)

_DEFAULT_CHAINS = [
    ModelChainDefinition(
        name="registration",
        head_topic=ROUTING_DECISION_TOPIC,
        tail_table="agent_routing_decisions",
        expected_fields=["correlation_id", "selected_agent"],
    ),
    ModelChainDefinition(
        name="pattern_learning",
        head_topic=PATTERN_STORED_TOPIC,
        tail_table="pattern_learning_artifacts",
        expected_fields=["pattern_id"],
    ),
    ModelChainDefinition(
        name="delegation",
        head_topic=TASK_DELEGATED_TOPIC,
        tail_table="delegation_events",
        expected_fields=["correlation_id"],
    ),
    ModelChainDefinition(
        name="routing",
        head_topic=LLM_ROUTING_DECISION_TOPIC,
        tail_table="llm_routing_decisions",
        expected_fields=["correlation_id"],
    ),
    ModelChainDefinition(
        name="evaluation",
        head_topic=RUN_EVALUATED_TOPIC,
        tail_table="session_outcomes",
        expected_fields=["correlation_id"],
    ),
]

_CHAIN_MAP = {c.name: c for c in _DEFAULT_CHAINS}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Validate golden chains from Kafka topics to DB projections."
    )
    parser.add_argument(
        "--chains",
        default="",
        help="Comma-separated chain names to validate (default: all 5 chains)",
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
        [_CHAIN_MAP[n] for n in chain_filter if n in _CHAIN_MAP]
        if chain_filter
        else _DEFAULT_CHAINS
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
