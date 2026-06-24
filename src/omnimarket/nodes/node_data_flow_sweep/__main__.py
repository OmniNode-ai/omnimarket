# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_data_flow_sweep.

Verifies end-to-end data flows from Kafka topics through DB projections.

Usage:
    # Live collection against a targeted lane — runs rpk/psql probes against the
    # lane's real broker + DB (recommended). The dev/stability/prod/judge lanes
    # run on .201 and are probed over SSH + docker exec (OMN-13552); --lane local
    # keeps the legacy in-stack docker/psql probe path.
    python -m omnimarket.nodes.node_data_flow_sweep --collect --lane dev

    # Live collection, single topic
    python -m omnimarket.nodes.node_data_flow_sweep --collect --lane dev --topic onex.evt.omniclaude.routing-decision.v1

    # Pre-collected metadata passed in (legacy / testing)
    python -m omnimarket.nodes.node_data_flow_sweep --flows '[{"topic": "...", ...}]'

    # Dry-run (no ticket creation)
    python -m omnimarket.nodes.node_data_flow_sweep --collect --lane dev --dry-run

The CLI and the RuntimeLocal dispatch path share flow resolution: both build a
``DataFlowSweepRequest`` whose ``collect`` field (when set, with an empty
``flows`` list) makes the handler's ``resolve_flows`` live-collect the built-in
critical-chain stubs. This is the OMN-13534 reconciliation — ``collect`` is a
real request-model field, not a ``__main__``-only argparse flag, so
``onex skill data_flow_sweep`` no longer crashes on a forbidden extra key and
the dispatch path actually probes.

Outputs JSON to stdout: DataFlowSweepResult model.
Exit 0 = healthy, exit 1 = issues found, exit 2 = target lane unreachable.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from pydantic import ValidationError

from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    _DEFAULT_FLOW_STUBS,
    DataFlowSweepRequest,
    DataFlowSweepResult,
    ModelFlowInput,
    NodeDataFlowSweep,
    _collect_live,
)
from omnimarket.nodes.node_data_flow_sweep.lane_target import (
    KNOWN_LANES,
    LaneResolutionError,
    resolve_lane_target,
)

_log = logging.getLogger(__name__)


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
        "--lane",
        default="dev",
        help=(
            "Lane to probe with --collect. One of: "
            f"local, {', '.join(KNOWN_LANES)}. The non-local lanes run on the "
            "runtime host (.201 by default) and are probed over SSH + docker "
            "exec; 'local' probes this host's in-stack docker/psql. "
            "Default: dev."
        ),
    )
    parser.add_argument(
        "--runtime-host",
        default="",
        help=(
            "Override the SSH host for a remote lane (defaults to the lane's "
            "canonical runtime host). Ignored for --lane local."
        ),
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
    # Build the flow list. The handler's resolve_flows() owns the empty-flows
    # default + collect path (shared with the dispatch path, OMN-13534); the
    # CLI only adds the --topic filter and explicit --flows parsing, then passes
    # a concrete flow list so request.collect stays False (collection already
    # happened here or is not requested).
    # -----------------------------------------------------------------------
    if args.collect:
        try:
            target = resolve_lane_target(
                args.lane, runtime_host=args.runtime_host or None
            )
        except LaneResolutionError as exc:
            _log.error("%s", exc)
            sys.exit(2)

        descriptors = list(_DEFAULT_FLOW_STUBS)
        if args.topic:
            descriptors = [d for d in descriptors if d.topic == args.topic]
            if not descriptors:
                _log.warning(
                    "no default flow descriptor found for topic %s; nothing to collect",
                    args.topic,
                )
        try:
            flows = _collect_live(
                descriptors, lane=target.lane, runtime_host=target.runtime_host
            )
        except Exception as exc:
            # OMN-13552: an unreachable / indeterminate target lane fails LOUD
            # as status=error (exit 2), never a false PRODUCER_DOWN or false-clean
            # verdict for a lane that was never actually probed.
            _log.error("lane %s unreachable: %s", target.lane, exc)
            error_result = DataFlowSweepResult(
                flow_results=[],
                flows_checked=len(descriptors),
                healthy=0,
                broken=0,
                status="error",
                dry_run=args.dry_run,
            )
            sys.stdout.write(error_result.model_dump_json(indent=2) + "\n")
            sys.exit(2)

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
        flows = list(_DEFAULT_FLOW_STUBS)
        if args.topic:
            flows = [f for f in flows if f.topic == args.topic]
            if not flows:
                _log.warning(
                    "no default flow found for topic %s; running with empty set",
                    args.topic,
                )

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
