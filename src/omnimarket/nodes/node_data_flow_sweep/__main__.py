# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_data_flow_sweep.

Verifies end-to-end data flows from Kafka topics through DB projections.

Usage:
    # Live collection — runs rpk/psql probes internally (recommended)
    python -m omnimarket.nodes.node_data_flow_sweep --collect

    # Live collection, single topic
    python -m omnimarket.nodes.node_data_flow_sweep --collect --topic onex.evt.omniclaude.routing-decision.v1

    # Pre-collected metadata passed in (legacy / testing)
    python -m omnimarket.nodes.node_data_flow_sweep --flows '[{"topic": "...", ...}]'

    # Dry-run (no ticket creation)
    python -m omnimarket.nodes.node_data_flow_sweep --collect --dry-run

Outputs JSON to stdout: DataFlowSweepResult model.
Exit 0 = healthy, exit 1 = issues found.
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

NODE_INTROSPECTION_TOPIC = "onex.evt.platform.node-introspection.v1"  # onex-topic-allow: pending contract auto-wiring
PATTERN_LEARNED_TOPIC = "onex.evt.omniintelligence.pattern-learned.v1"  # onex-topic-allow: pending contract auto-wiring
ROUTING_DECISION_TOPIC = "onex.evt.omniclaude.routing-decision.v1"  # onex-topic-allow: pending contract auto-wiring

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default flow stubs — topology only, no live metadata.
# When --collect is set, each stub is populated via collector.collect_flow_metadata().
# ---------------------------------------------------------------------------

_DEFAULT_FLOW_STUBS = [
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

# Keep _DEFAULT_FLOWS as a backwards-compatible alias (same objects — stubs
# carry safe zero-value defaults so pure-compute tests keep passing).
_DEFAULT_FLOWS = _DEFAULT_FLOW_STUBS


def _collect_live(descriptors: list[ModelFlowInput]) -> list[ModelFlowInput]:
    """Populate live rpk/psql metadata for each flow descriptor.

    Imported lazily so that tests importing only the handler never pull in
    subprocess/shell dependencies.  Falls back to the original descriptor on
    per-flow collection failure so a single unreachable topic does not abort
    the entire sweep.
    """
    from omnimarket.nodes.node_data_flow_sweep.collector import collect_flow_metadata

    populated: list[ModelFlowInput] = []
    for descriptor in descriptors:
        try:
            populated.append(collect_flow_metadata(descriptor))
        except Exception as exc:
            _log.warning(
                "collection failed for %s: %s — using descriptor", descriptor.topic, exc
            )
            populated.append(descriptor)
    return populated


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Verify end-to-end data flows from Kafka to DB projections."
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        default=False,
        help=(
            "Run live rpk/psql probes to collect producer status, consumer lag, "
            "and DB row counts.  Mutually exclusive with --flows."
        ),
    )
    parser.add_argument(
        "--flows",
        default="",
        help=(
            "JSON array of pre-collected flow objects (keys: topic, handler_name, "
            "table_name, dashboard_route, producer_status, consumer_lag, "
            "table_row_count, table_has_recent_data, field_mapping_valid). "
            "Default when --collect is not set: built-in critical chains."
        ),
    )
    parser.add_argument(
        "--topic",
        default="",
        help="Filter to a single topic name.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Verify and report only — no ticket creation.",
    )

    args = parser.parse_args()

    if args.collect and args.flows:
        _log.error("--collect and --flows are mutually exclusive")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Build the flow list
    # -----------------------------------------------------------------------
    if args.collect:
        descriptors = list(_DEFAULT_FLOW_STUBS)
        if args.topic:
            descriptors = [d for d in descriptors if d.topic == args.topic]
            if not descriptors:
                _log.warning(
                    "no default flow descriptor found for topic %s; nothing to collect",
                    args.topic,
                )
        flows = _collect_live(descriptors)

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
        if args.topic:
            flows = [f for f in flows if f.topic == args.topic]

    else:
        # Legacy / no-flag path: use descriptors as-is (zero-value defaults)
        flows = list(_DEFAULT_FLOW_STUBS)
        if args.topic:
            flows = [f for f in flows if f.topic == args.topic]
            if not flows:
                _log.warning(
                    "no default flow found for topic %s; running with empty set",
                    args.topic,
                )

    # -----------------------------------------------------------------------
    # Run the pure compute handler
    # -----------------------------------------------------------------------
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
