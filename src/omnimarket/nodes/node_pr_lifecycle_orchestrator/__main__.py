# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_pr_lifecycle_orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

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

    args = parser.parse_args()
    try:
        command = _parse_command(args.input)
    except Exception as exc:
        parser.error(str(exc))

    result = asyncio.run(_run(command))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    if result.final_state == "FAILED":
        sys.exit(1)


if __name__ == "__main__":
    main()
