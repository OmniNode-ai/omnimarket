"""CLI entry point for node_bus_audit_compute."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_bus_audit_compute.handlers.handler_bus_audit_compute import (
    HandlerBusAuditCompute,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_request import (
    ModelBusAuditComputeRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit event bus topic registry and node contract wiring."
    )
    parser.add_argument("--scope", default="local", help="Operator-facing audit scope.")
    parser.add_argument(
        "--registry-path", help="Path to the event registry topics.yaml file."
    )
    parser.add_argument(
        "--contract-root",
        action="append",
        default=[],
        help="Contract root or contract.yaml file to scan. Can be repeated.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.add_argument(
        "--failures-only", action="store_true", help="Return only ERROR findings."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Include informational findings."
    )
    parser.add_argument(
        "--skip-daemon",
        action="store_true",
        help="Skip live daemon checks. This node currently performs static audit only.",
    )
    parser.add_argument(
        "--broker",
        help="Kafka broker hint for future live sampling; not contacted by this node.",
    )
    parser.add_argument(
        "--sample-count",
        type=int,
        default=20,
        help="Requested sample count for future live topic sampling.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run without effects.")
    args = parser.parse_args(argv)

    request = ModelBusAuditComputeRequest(
        scope=args.scope,
        registry_path=args.registry_path,
        contract_roots=[str(Path(item)) for item in args.contract_root],
        failures_only=args.failures_only,
        verbose=args.verbose,
        skip_daemon=args.skip_daemon,
        broker=args.broker,
        sample_count=args.sample_count,
        dry_run=args.dry_run,
    )
    result = HandlerBusAuditCompute().handle(request)

    # The skill contract asks for JSON stdout. Keep --json for compatibility,
    # but emit JSON unconditionally so `onex run` has one stable parse surface.
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
