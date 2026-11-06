# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host timer ingress: wire the declared node, publish a tick, read its terminal."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.scm import ModelScmTopics
from omnimarket.nodes.node_item_tick_orchestrator.handlers.handler_item_tick import (
    CONTRACT,
    liveness_overlay,
    load_config,
    policy,
)
from omnimarket.nodes.node_item_tick_orchestrator.models.model_item_tick import (
    ModelItemTickConfig,
    ModelItemTickRequest,
    ModelItemTickResult,
)


async def dispatch(
    config: ModelItemTickConfig, registry: Path | None
) -> ModelItemTickResult:
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    # Bind the existing opener in its declared parameter order, including its registry root.
    arguments = dict(
        zip(
            inspect.signature(open_lab_run_bus).parameters,
            (config.bus, config.lane, None, registry),
            strict=True,
        )
    )
    async with opener(**arguments) as bus:
        if config.bus == "inmemory":
            manifest = discover_contracts_from_paths([CONTRACT])
            if manifest.errors or len(manifest.contracts) != 1:
                raise RuntimeError(
                    f"item tick contract discovery refused: {manifest.errors}"
                )
            engine = MessageDispatchEngine()
            report = await wire_from_manifest(
                manifest, engine, event_bus=bus, environment="local"
            )
            if any(result.outcome.value != "wired" for result in report.results):
                raise RuntimeError(f"item tick wiring refused: {report}")
            engine.freeze()
        # On Redpanda this is a client. The declared effects runtime hosts the
        # orchestrator, so the timer never adds a second consumer of its command.
        document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        request = ModelItemTickRequest(config=config)
        terminal = await ScmBusCaller(bus).call_topics(
            topics=ModelScmTopics(
                command=document["runtime_dispatch"]["command_topic"],
                success=document["terminal_event"],
            ),
            request=request,
            correlation_id=request.correlation_id,
            timeout_s=policy().runtime_timeout_seconds,
        )
        if terminal.payload.get("status") == "failed":
            raise RuntimeError(
                f"item tick handler refused: {terminal.payload.get('failure_reason')}"
            )
        return ModelItemTickResult.model_validate(terminal.payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--overlay", type=Path, default=os.environ.get("ONEX_ITEM_TICK_OVERLAY")
    )
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--print-liveness-overlay", action="store_true")
    args = parser.parse_args(argv)
    if args.overlay is None:
        parser.error("set ONEX_ITEM_TICK_OVERLAY or pass --overlay")
    try:
        config = load_config(args.overlay)
        if args.print_liveness_overlay:
            sys.stdout.write(liveness_overlay(config).model_dump_json(indent=2) + "\n")
            return 0
        if args.check_config:
            sys.stdout.write("ITEM-TICK CONFIGURED: " + ", ".join(config.checks) + "\n")
            sys.stdout.flush()
            return 0
        root = os.environ.get("OMNI_HOME")
        sys.stdout.write(
            f"ITEM-TICK STARTED host={config.host} at={datetime.now(UTC).isoformat()}\n"
        )
        sys.stdout.flush()
        result = asyncio.run(dispatch(config, Path(root) if root else None))
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"ITEM-TICK REFUSED: {exc}\n")
        sys.stderr.flush()
        return 1
    for line in result.lines:
        sys.stdout.write(line + "\n")
    sys.stdout.write(result.model_dump_json() + "\n")
    sys.stdout.flush()
    return 1 if result.status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
