from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
    HandlerGapCompute,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    ModelGapComputeRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic gap compute.")
    parser.add_argument(
        "subcommand",
        nargs="?",
        default="detect",
        choices=["detect", "fix", "cycle", "reconcile"],
    )
    parser.add_argument("--scope", default="local")
    parser.add_argument("--epic")
    parser.add_argument("--report")
    parser.add_argument("--repo")
    parser.add_argument("--repo-root", action="append", default=[])
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument(
        "--severity-threshold", choices=["WARNING", "CRITICAL"], default="WARNING"
    )
    parser.add_argument("--max-findings", type=int, default=200)
    parser.add_argument("--max-best-effort", type=int, default=50)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--output", choices=["json"], default="json")
    parser.add_argument("--ticket")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--mode", default="ticket-pipeline")
    parser.add_argument("--choose")
    parser.add_argument("--force-decide", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--no-fix", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--auto-only", action="store_true")
    parser.add_argument("--skip-infra-probes", action="store_true")
    parser.add_argument("--include-auth-probes", action="store_true")
    parser.add_argument("--lag-threshold", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    request = ModelGapComputeRequest(
        subcommand=args.subcommand,
        scope=args.scope,
        epic=args.epic,
        report=args.report,
        repo=args.repo,
        repo_roots=[str(Path(item)) for item in args.repo_root],
        since_days=args.since_days,
        severity_threshold=args.severity_threshold,
        max_findings=args.max_findings,
        max_best_effort=args.max_best_effort,
        max_iterations=args.max_iterations,
        output=args.output,
        ticket=args.ticket,
        latest=args.latest,
        mode=args.mode,
        choose=args.choose,
        force_decide=args.force_decide,
        resume=args.resume,
        audit=args.audit,
        no_fix=args.no_fix,
        verify=args.verify,
        auto_only=args.auto_only,
        skip_infra_probes=args.skip_infra_probes,
        include_auth_probes=args.include_auth_probes,
        lag_threshold=args.lag_threshold,
        dry_run=args.dry_run,
    )
    result = HandlerGapCompute().handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
