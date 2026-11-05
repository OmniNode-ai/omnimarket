# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_run_engine_effect.backends.worker_temporal``: the worker of the Temporal overlay.

Reads the node overlay (``node_run_engine_effect`` under ``ONEX_SKILL_OVERLAY_ROOTS``), connects to the server the
overlay names and runs command steps until killed. It is the only process that executes a step; the server holds the
order of work and the waits.
"""

from __future__ import annotations

import asyncio
import sys

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_run_engine_effect.handlers.overlay_run_engine import (
    NODE_OVERLAY_ID,
    load_run_engine_overlay,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumRunEngineBackend,
    ModelTemporalSelfHostedSettings,
)


async def run_worker(settings: ModelTemporalSelfHostedSettings) -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    from omnimarket.nodes.node_run_engine_effect.backends.temporal_definitions import (
        RunEngineWorkflow,
        run_engine_command_step,
    )

    client = await Client.connect(settings.target_host, namespace=settings.namespace)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[RunEngineWorkflow],
        activities=[run_engine_command_step],
    )
    await worker.run()


def main() -> int:
    try:
        overlay = load_run_engine_overlay()
    except NodeOverlayError as refused:
        sys.stderr.write(f"refused: {refused}\n")
        return 1
    settings = overlay.temporal_self_hosted
    if (
        overlay.run_engine_backend is not EnumRunEngineBackend.TEMPORAL_SELF_HOSTED
        or settings is None
    ):
        sys.stderr.write(
            f"refused: {NODE_OVERLAY_ID} does not select temporal_self_hosted\n"
        )
        return 1
    asyncio.run(run_worker(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
