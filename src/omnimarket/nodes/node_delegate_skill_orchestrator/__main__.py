# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI proof surface for node_delegate_skill_orchestrator."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from uuid import UUID

from omnimarket.adapters.claude_code.delegate import DelegationDispatchAdapter
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
from omnimarket.models.cli_report import ModelMarketCliStep


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a typed delegate-skill command envelope."
    )
    parser.add_argument("--prompt", required=True, help="User prompt to delegate.")
    parser.add_argument("--task-type", required=True, help="Delegation task class.")
    parser.add_argument(
        "--source",
        default="codex",
        help="Registered delegation caller source.",
    )
    parser.add_argument("--cwd", default=None, help="Originating working directory.")
    parser.add_argument("--source-file", default=None, help="Source file context.")
    parser.add_argument(
        "--working-directory",
        default=None,
        help="Worker working directory context.",
    )
    parser.add_argument("--session-id", default=None, help="Originating session ID.")
    parser.add_argument("--recipient", default="codex", help="Delegation recipient.")
    parser.add_argument(
        "--codex-sandbox-mode",
        default=None,
        help="Codex sandbox mode requested by the caller.",
    )
    parser.add_argument(
        "--quality-contract-mode",
        choices=("extend_task_class", "replace_task_class"),
        default="extend_task_class",
    )
    parser.add_argument(
        "--acceptance-criterion",
        action="append",
        default=[],
        help="Request-level quality criterion. May be repeated.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help=(
            "Optional explicit output-token budget. Omit to resolve the effective "
            "value from the selected backend's per-backend ceiling (OMN-13161)."
        ),
    )
    parser.add_argument("--correlation-id", default=None)
    parser.add_argument(
        "--dispatch",
        action="store_true",
        default=False,
        help="Publish to the runtime instead of compile-only validation.",
    )
    parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Do not wait for a synchronous result when dispatching.",
    )
    parser.set_defaults(wait=True)
    add_output_args(parser)
    return parser


def _result_summary(envelope: dict[str, object]) -> dict[str, object]:
    payload = envelope.get("payload")
    delegation_payload = payload if isinstance(payload, dict) else {}
    terminal_events = envelope.get("terminal_events")
    return {
        "ok": envelope.get("ok"),
        "command_topic": envelope.get("command_topic"),
        "task_type": delegation_payload.get("task_type"),
        "source": delegation_payload.get("source"),
        "recipient": delegation_payload.get("recipient"),
        "model_selected": envelope.get("model_name"),
        "model_cloud_baseline": envelope.get("model_cloud_baseline"),
        "pricing_manifest_version": envelope.get("pricing_manifest_version"),
        "frontier_costs_usd": envelope.get("frontier_costs_usd"),
        "terminal_events_configured": isinstance(terminal_events, dict),
    }


def _compile_or_dispatch(args: argparse.Namespace) -> dict[str, object]:
    adapter = DelegationDispatchAdapter()
    common_kwargs = {
        "prompt": str(args.prompt),
        "task_type": str(args.task_type),
        "source": str(args.source),
        "cwd": args.cwd,
        "source_file_path": args.source_file,
        "working_directory": args.working_directory,
        "session_id": args.session_id,
        "recipient": args.recipient,
        "codex_sandbox_mode": args.codex_sandbox_mode,
        "quality_contract_mode": args.quality_contract_mode,
        "acceptance_criteria": tuple(str(item) for item in args.acceptance_criterion),
        "wait": bool(args.wait),
        # OMN-13161: None => omit from payload so the backend ceiling resolves.
        "max_tokens": None if args.max_tokens is None else int(args.max_tokens),
        "correlation_id": args.correlation_id,
    }
    if args.dispatch:
        result = adapter.dispatch_sync(**common_kwargs)
        return {str(key): value for key, value in result.items()}
    envelope = adapter.compile_request(**common_kwargs)
    return {"ok": True, **envelope}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.getLogger().setLevel(args.log_level.upper())

    try:
        result = _compile_or_dispatch(args)
    except ValueError as exc:
        correlation_id = args.correlation_id
        payload = {"ok": False, "correlation_id": correlation_id, "error": str(exc)}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 2
    except Exception as exc:
        correlation_id = args.correlation_id
        payload = {"ok": False, "correlation_id": correlation_id, "error": str(exc)}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 1

    if not report_output_requested(argv):
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        return 0 if result.get("ok") is True else 1

    contract_name, contract_version, terminal_event = load_contract_metadata(
        "omnimarket.nodes.node_delegate_skill_orchestrator"
    )
    output_config = resolve_output_config(args)
    cli_report = build_report_from_model_result(
        result,
        skill_name="delegate_skill",
        node_name="node_delegate_skill_orchestrator",
        terminal_event=terminal_event,
        contract_name=contract_name,
        contract_version=contract_version,
        mode="dispatch" if args.dispatch else "compile_only",
        input_summary={
            "task_type": args.task_type,
            "source": args.source,
            "recipient": args.recipient,
            "wait": args.wait,
            "max_tokens": args.max_tokens,
            "dispatch": args.dispatch,
            "quality_contract_mode": args.quality_contract_mode,
            "acceptance_criteria_count": len(args.acceptance_criterion),
        },
        output_config=output_config,
        result_summary=_result_summary(result),
        steps=[
            ModelMarketCliStep(
                name="compile_delegate_skill_command",
                status="success",
                description="Validated typed command envelope from contract routing.",
                details={
                    "command_topic": result.get("command_topic"),
                    "correlation_id": str(result.get("correlation_id", "")),
                },
            )
        ],
    )
    rendered = resolve_handler(output_config.format).render(cli_report)
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")
    correlation_id = cli_report.correlation_id
    if not isinstance(correlation_id, UUID):
        return 1
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
