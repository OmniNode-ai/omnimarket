# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_session_orchestrator.

Usage:
    python -m omnimarket.nodes.node_session_orchestrator [options]

Options:
    --mode          interactive | autonomous (default: interactive)
    --phase         Run only phase 1, 2, or 3 (default: 0 = all)
    --dry-run       Print plan without dispatching
    --skip-health   Skip Phase 1 health gate (emergency only)
    --state-dir     Path to session state directory
    --output-json   Print result as JSON to stdout
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    HandlerSessionOrchestrator,
    ModelSessionOrchestratorCommand,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="node_session_orchestrator — OMN-8367 PoC"
    )
    parser.add_argument(
        "--mode", default="interactive", choices=["interactive", "autonomous"]
    )
    parser.add_argument("--phase", type=int, default=0, choices=[0, 1, 2, 3])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--state-dir", default=".onex_state/session")
    parser.add_argument("--output-json", action="store_true")
    parser.add_argument("--session-id", default="")
    return parser.parse_args(argv)


def _emit(msg: str) -> None:
    sys.stdout.write(msg + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    command = ModelSessionOrchestratorCommand(
        session_id=args.session_id,
        mode=args.mode,
        dry_run=args.dry_run,
        skip_health=args.skip_health,
        state_dir=args.state_dir,
        phase=args.phase,
    )
    handler = HandlerSessionOrchestrator()
    result = handler.handle(command)

    if args.output_json:
        _emit(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        _emit("\n=== node_session_orchestrator result ===")
        _emit(f"session_id  : {result.session_id}")
        _emit(f"status      : {result.status}")
        _emit(f"halt_reason : {result.halt_reason or '(none)'}")
        if result.health_report:
            report = result.health_report
            _emit(f"\nHealth gate : {report.overall_status} / {report.gate_decision}")
            for dim in report.dimensions:
                flag = (
                    " [BLOCKS]"
                    if dim.blocks_dispatch and dim.status.value != "GREEN"
                    else ""
                )
                _emit(f"  [{dim.status.value:6}] {dim.dimension}{flag}")
                for item in dim.actionable_items:
                    _emit(f"           -> {item}")
        _emit(f"\ndry_run     : {result.dry_run}")
        _emit("========================================\n")

    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
