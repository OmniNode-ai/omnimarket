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

from omnimarket.cli.args import (
    add_output_args,
    report_output_requested,
    resolve_output_config,
)
from omnimarket.cli.output.registry import resolve_handler
from omnimarket.cli.reporting import (
    build_report_from_model_result,
    load_contract_metadata,
)
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
    add_output_args(parser)
    return parser.parse_args(argv)


def _emit(msg: str) -> None:
    sys.stdout.write(msg + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_config = resolve_output_config(args)
    logging.getLogger().setLevel(args.log_level.upper())
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
    elif report_output_requested(argv):
        contract_name, contract_version, terminal_event = load_contract_metadata(
            "omnimarket.nodes.node_session_orchestrator"
        )
        cli_report = build_report_from_model_result(
            result,
            skill_name="session_orchestrator",
            node_name="node_session_orchestrator",
            terminal_event=terminal_event,
            contract_name=contract_name,
            contract_version=contract_version,
            mode="dry_run" if args.dry_run else "execute",
            input_summary={
                "session_id": args.session_id,
                "mode": args.mode,
                "phase": args.phase,
                "dry_run": args.dry_run,
                "skip_health": args.skip_health,
                "state_dir": args.state_dir,
            },
            output_config=output_config,
        )
        rendered = resolve_handler(output_config.format).render(cli_report)
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
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
