# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner read worker of a lab host, run by its registry_module_tick unit (merged-change-runner-read-systemd).

Creates or updates the Temporal ready-edge schedule (the contract's ``runner_read`` block), then runs the worker
until it exits; the unit's timer starts it again within its interval of any exit, so a worker that starts before the
server answers retries on the next tick. ``--probe`` checks the imports and returns without a connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from omnimarket.nodes.node_merged_change_runner_read_workflow.runtime_runner_read import (
    connect,
    contract_config,
    run_worker,
    upsert_schedule,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_runner_read_worker")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    cfg = contract_config()
    address = os.environ.get("ONEX_TEMPORAL_ADDRESS") or str(cfg["address"])
    namespace = os.environ.get("ONEX_TEMPORAL_NAMESPACE") or str(cfg["namespace"])
    queue = str(cfg["task_queue"])
    if args.probe:
        from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers import (
            temporal_runner_read,
        )
        from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_ready_edge import (
            READY_WORKFLOW_NAME,
        )

        sys.stdout.write(
            json.dumps(
                {
                    "probe": "ok",
                    "workflow": temporal_runner_read.WORKFLOW_NAME,
                    "watch": READY_WORKFLOW_NAME,
                }
            )
            + "\n"
        )
        return 0

    async def _schedule() -> dict[str, str]:
        return await upsert_schedule(await connect(address, namespace), cfg, queue)

    sys.stdout.write(json.dumps(asyncio.run(_schedule())) + "\n")
    sys.stdout.flush()
    asyncio.run(run_worker(address, namespace, queue))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
