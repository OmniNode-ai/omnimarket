# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_verification_sweep_orchestrator.

Usage:
    python -m omnimarket.nodes.node_verification_sweep_orchestrator --targets OMN-1,OMN-2
    python -m omnimarket.nodes.node_verification_sweep_orchestrator --epic OMN-EPIC --dry-run

Outputs JSON to stdout: ModelVerificationSweepOrchestratorResult.

Coverage boundary (OMN-14552): this bare ``python -m`` entry point exercises
dispatch + the fail-closed census gate + dry-run planning; it does NOT inject
the live probe / receipt / Linear adapters. Live dashboard/database/dod probing
is wired by the runtime on the ``onex skill verification_sweep`` path (the
contract declares the adapter dependencies). Without an injected probe adapter,
any resolved target fails closed with a typed adapter error rather than a silent
pass — an honest "cannot verify" over a "verified nothing".
"""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_verification_sweep_orchestrator.handlers.handler_verification_sweep_orchestrator import (
    HandlerVerificationSweepOrchestrator,
)
from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
    ModelVerificationSweepOrchestratorRequest,
)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-orchestration verification sweep over a target set."
    )
    parser.add_argument(
        "--targets",
        default="",
        help="Comma-separated ticket IDs to verify (e.g. OMN-1,OMN-2).",
    )
    parser.add_argument(
        "--epic",
        default=None,
        help="Epic ID — discover and verify all child tickets.",
    )
    parser.add_argument(
        "--check-types",
        default="",
        help="Comma-separated phases: dashboard,database,dod_evidence (default: all).",
    )
    parser.add_argument(
        "--pr",
        default=None,
        help="GitHub PR reference (owner/repo#number) for pre-merge mode.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="Hard timeout for a single pre-merge PR verification run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing receipts or Linear comments.",
    )

    args = parser.parse_args()

    request = ModelVerificationSweepOrchestratorRequest(
        targets=_split_csv(args.targets),
        epic=args.epic,
        check_types=_split_csv(args.check_types),
        pr=args.pr,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )

    handler = HandlerVerificationSweepOrchestrator()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    # Fail-closed exit: a non-``pass`` verdict (including an empty-scope refusal)
    # exits non-zero so a caller/CI never mistakes "verified nothing" for green.
    if result.overall_status != "pass":
        sys.exit(1)


if __name__ == "__main__":
    main()
