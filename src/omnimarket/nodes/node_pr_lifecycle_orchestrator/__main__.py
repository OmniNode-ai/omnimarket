# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_pr_lifecycle_orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

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
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleResult,
    ModelPrLifecycleStartCommand,
)

EVENT_TYPE_PR_LIFECYCLE_START = "omnimarket.pr-lifecycle-orchestrator-start"


def _parse_command(input_json: str) -> ModelPrLifecycleStartCommand:
    envelope = ModelEventEnvelope[object].model_validate_json(input_json)
    if envelope.event_type != EVENT_TYPE_PR_LIFECYCLE_START:
        msg = (
            f"--input event_type must be {EVENT_TYPE_PR_LIFECYCLE_START!r}; "
            f"got {envelope.event_type!r}"
        )
        raise argparse.ArgumentTypeError(msg)
    return ModelPrLifecycleStartCommand.model_validate(envelope.payload)


async def _run(command: ModelPrLifecycleStartCommand) -> ModelPrLifecycleResult:
    handler = HandlerPrLifecycleOrchestrator()
    return await handler.handle(command)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Run the PR lifecycle orchestrator from a contract-canonical "
            "ModelEventEnvelope JSON payload."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help=(
            "Raw JSON ModelEventEnvelope payload with event_type "
            f"{EVENT_TYPE_PR_LIFECYCLE_START!r}."
        ),
    )
    add_output_args(parser)

    args = parser.parse_args()
    output_config = resolve_output_config(args)
    logging.getLogger().setLevel(args.log_level.upper())
    try:
        command = _parse_command(args.input)
    except Exception as exc:
        parser.error(str(exc))

    result = asyncio.run(_run(command))
    if not report_output_requested():
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        if result.final_state == "FAILED":
            sys.exit(1)
        return

    contract_name, contract_version, terminal_event = load_contract_metadata(
        "omnimarket.nodes.node_pr_lifecycle_orchestrator"
    )
    cli_report = build_report_from_model_result(
        result,
        skill_name="pr_lifecycle_orchestrator",
        node_name="node_pr_lifecycle_orchestrator",
        terminal_event=terminal_event,
        contract_name=contract_name,
        contract_version=contract_version,
        mode="dry_run" if command.dry_run else "execute",
        input_summary={
            "run_id": command.run_id,
            "dry_run": command.dry_run,
            "inventory_only": command.inventory_only,
        },
        output_config=output_config,
    )
    rendered = resolve_handler(output_config.format).render(cli_report)
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")
    if result.final_state == "FAILED":
        sys.exit(1)


if __name__ == "__main__":
    main()
