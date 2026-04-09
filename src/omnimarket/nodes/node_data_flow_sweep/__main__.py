# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_data_flow_sweep.

Verifies end-to-end data flows from Kafka topics through DB projections.
Flow metadata is passed via --flows JSON array.

Usage:
    python -m omnimarket.nodes.node_data_flow_sweep \
        --topic onex.evt.omniclaude.routing-decision.v1 \
        --dry-run

Outputs JSON to stdout: DataFlowSweepResult model.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from pydantic import ValidationError

from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    DataFlowSweepRequest,
    ModelFlowInput,
    NodeDataFlowSweep,
)
from omnimarket.nodes.node_data_flow_sweep.topics import (
    NODE_INTROSPECTION_TOPIC,
    PATTERN_LEARNED_TOPIC,
    ROUTING_DECISION_TOPIC,
)

_log = logging.getLogger(__name__)

_DEFAULT_FLOWS = [
    ModelFlowInput(
        topic=NODE_INTROSPECTION_TOPIC,
        handler_name="projectNodeIntrospection",
        table_name="node_service_registry",
        dashboard_route="/agents",
    ),
    ModelFlowInput(
        topic=PATTERN_LEARNED_TOPIC,
        handler_name="projectPatternLearned",
        table_name="pattern_learning_artifacts",
        dashboard_route="/intelligence",
    ),
    ModelFlowInput(
        topic=ROUTING_DECISION_TOPIC,
        handler_name="projectRoutingDecision",
        table_name="agent_routing_decisions",
        dashboard_route="/pipeline",
    ),
]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Verify end-to-end data flows from Kafka to DB projections."
    )
    parser.add_argument(
        "--flows",
        default="",
        help=(
            "JSON array of flow objects (keys: topic, handler_name, table_name, "
            "dashboard_route, producer_status, consumer_lag, table_row_count, "
            "table_has_recent_data, field_mapping_valid). "
            "Default: built-in critical chains."
        ),
    )
    parser.add_argument(
        "--topic",
        default="",
        help="Filter to a single topic name (overrides --flows if set)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Verify and report only — no ticket creation",
    )

    args = parser.parse_args()

    if args.topic:
        flows = [f for f in _DEFAULT_FLOWS if f.topic == args.topic]
        if not flows:
            _log.warning(
                "no default flow found for topic %s; running with empty set", args.topic
            )
    elif args.flows:
        try:
            raw_flows: list[dict[str, object]] = json.loads(args.flows)
        except json.JSONDecodeError as exc:
            _log.error("invalid --flows JSON: %s", exc)
            sys.exit(1)
        try:
            flows = [ModelFlowInput.model_validate(f) for f in raw_flows]
        except ValidationError as exc:
            _log.error("invalid --flows content: %s", exc)
            sys.exit(1)
    else:
        flows = _DEFAULT_FLOWS

    request = DataFlowSweepRequest(
        flows=flows,
        dry_run=args.dry_run,
    )

    handler = NodeDataFlowSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("healthy",):
        sys.exit(1)


if __name__ == "__main__":
    main()
