# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assemble the build loop orchestrator with live adapters.

Wires the orchestrator to:
- Linear API for ticket fill (Active Sprint, Backlog/Todo)
- Local Qwen3-14B for ticket classification (with keyword fallback)
- Multi-model delegation for code generation:
  - Simple tasks -> local Qwen3-14B
  - Medium tasks -> local Qwen3-Coder-30B (64K ctx)
  - Complex tasks -> frontier Gemini / OpenAI
- DeepSeek-R1 for code review verification
- Passthrough closeout/verify (run via separate skills)

Usage:
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.assemble_live import (
        assemble_live_orchestrator,
    )

    orchestrator = assemble_live_orchestrator()
    result = await orchestrator.handle(command)

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    build_endpoint_configs,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_linear_fill import (
    AdapterLinearFill,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_classify import (
    AdapterLlmClassify,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    AdapterLlmDispatch,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    CloseoutResult,
    VerifyResult,
)

logger = logging.getLogger(__name__)


class _PassthroughCloseout:
    """Minimal closeout that always succeeds (closeout runs via separate skill)."""

    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> CloseoutResult:
        logger.info("Closeout: passthrough (correlation_id=%s)", correlation_id)
        return CloseoutResult(success=True)


class _PassthroughVerify:
    """Minimal verify that always passes (verification runs via separate skill)."""

    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> VerifyResult:
        logger.info("Verify: passthrough (correlation_id=%s)", correlation_id)
        return VerifyResult(all_critical_passed=True)


def assemble_live_orchestrator(
    *,
    linear_api_key: str | None = None,
    linear_team_id: str | None = None,
    linear_team_key: str | None = None,
    classify_url: str | None = None,
) -> HandlerBuildLoopOrchestrator:
    """Assemble the orchestrator with live Linear + multi-model LLM adapters.

    All parameters are optional — defaults come from environment variables.
    Frontier model availability is determined by API key presence in env.
    """
    endpoint_configs = build_endpoint_configs()

    logger.info(
        "Assembling live orchestrator with %d model tiers: %s",
        len(endpoint_configs),
        ", ".join(t.value for t in sorted(endpoint_configs.keys(), key=str)),
    )

    return HandlerBuildLoopOrchestrator(
        closeout=_PassthroughCloseout(),
        verify=_PassthroughVerify(),
        rsd_fill=AdapterLinearFill(
            api_key=linear_api_key,
            team_id=linear_team_id,
            team_key=linear_team_key,
        ),
        classify=AdapterLlmClassify(llm_url=classify_url),
        dispatch=AdapterLlmDispatch(endpoint_configs=endpoint_configs),
    )


async def run_live_build_loop(
    *,
    max_cycles: int = 1,
    max_tickets: int = 50,
    mode: str = "build",
    dry_run: bool = False,
    skip_closeout: bool = True,
    output_dir: str | None = None,
) -> None:
    """Run the live build loop end-to-end.

    Convenience function for CLI invocation. Writes results to disk.

    Args:
        max_cycles: Number of build loop cycles.
        max_tickets: Max tickets to pull from Linear per cycle.
        mode: Execution mode (build, close_out, full, observe).
        dry_run: Simulate without side effects.
        skip_closeout: Skip the CLOSING_OUT phase.
        output_dir: Directory to write results to.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    orchestrator = assemble_live_orchestrator()
    command = ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=max_cycles,
        max_tickets=max_tickets,
        mode=mode,
        dry_run=dry_run,
        skip_closeout=skip_closeout,
        requested_at=datetime.now(tz=UTC),
    )

    logger.info(
        "Starting live build loop (max_cycles=%d, max_tickets=%d, mode=%s, dry_run=%s)",
        max_cycles,
        max_tickets,
        mode,
        dry_run,
    )

    result = await orchestrator.handle(command)

    logger.info(
        "Build loop complete: %d cycles completed, %d failed, %d tickets dispatched",
        result.cycles_completed,
        result.cycles_failed,
        result.total_tickets_dispatched,
    )

    for summary in result.cycle_summaries:
        logger.info(
            "  Cycle %d: phase=%s, filled=%d, classified=%d, dispatched=%d",
            summary.cycle_number,
            summary.final_phase.value,
            summary.tickets_filled,
            summary.tickets_classified,
            summary.tickets_dispatched,
        )

    # Write results to disk
    _default_output = os.environ.get(
        "BUILD_LOOP_OUTPUT_DIR",
        str(Path.home() / ".onex_state" / "build-loop-results"),
    )
    out_path = Path(output_dir or _default_output)
    out_path.mkdir(parents=True, exist_ok=True)
    result_file = out_path / f"live-run-{command.correlation_id}.json"
    result_file.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, default=str)
    )
    logger.info("Results written to %s", result_file)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run live build loop")
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--max-tickets", type=int, default=50)
    parser.add_argument("--mode", default="build")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-closeout", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    asyncio.run(
        run_live_build_loop(
            max_cycles=args.max_cycles,
            max_tickets=args.max_tickets,
            mode=args.mode,
            dry_run=args.dry_run,
            skip_closeout=args.skip_closeout,
            output_dir=args.output_dir,
        )
    )


__all__: list[str] = ["assemble_live_orchestrator", "run_live_build_loop"]
