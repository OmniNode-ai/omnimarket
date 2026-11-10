# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of a host's declared-clone refresh, the unit the host installer renders (OMN-23557).

``python -m omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick``
``--omni-home ROOT --state-dir DIR`` runs the EXISTING declared-clone refresh for this host's own row
and writes ``DIR/last-run.json`` as the unit's evidence. Exit 0 when the refresh is clean, 1 when any
host is a refusal, 3 when the tick could not be judged (no overlay, a bus that will not open, a
receipt that never arrived in time).
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, open_lab_run_bus
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.handlers.overlay_keys import (
    resolve_lab_hosts_table,
    resolve_overlay_key,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.adapter_declared_clone_refresh_host import (
    _REGISTRY_KW,
    load_declared_clone_topics,
    run_declared_refresh,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
    TransportSubprocess,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    ModelDeclaredCloneRefreshReceipt,
    ModelDeclaredCloneRefreshRequest,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_tick import (
    ModelDeclaredCloneTickOverlay,
)

NODE = "node_canonical_clone_refresh_effect"
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INDETERMINATE = 3


def host_name(overlay: ModelDeclaredCloneTickOverlay, env: Mapping[str, str]) -> str:
    """This host's name from the environment variable the overlay names; a blank one is a reading."""
    name = env.get(overlay.host_name_env, "").strip()
    if not name:
        raise ValueError(f"{overlay.host_name_env} is not set or is blank")
    return name


def exit_code(receipt: ModelDeclaredCloneRefreshReceipt) -> int:
    """1 when any host is a refusal, 0 when the refresh is clean."""
    return EXIT_REFUSED if receipt.refused else EXIT_OK


def write_evidence(
    state_dir: Path,
    host: str,
    receipt: ModelDeclaredCloneRefreshReceipt | None,
    code: int,
) -> None:
    """The unit's evidence, written after every tick and replaced whole."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "last-run.json"
    temporary = state_dir / f".last-run.{os.getpid()}.json"
    outcomes = []
    if receipt is not None:
        for outcome in receipt.outcomes:
            outcomes.append(
                {
                    "host": outcome.host,
                    "path": outcome.path,
                    "state": outcome.state.value,
                    "refusal": outcome.refusal.value if outcome.refusal else None,
                    "before_sha": outcome.before_sha,
                    "after_sha": outcome.after_sha,
                    "origin_sha": outcome.origin_sha,
                    "message": outcome.message,
                }
            )
    temporary.write_text(
        json.dumps(
            {
                "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "exit_code": code,
                "host": host,
                "refused": True if receipt is None else receipt.refused,
                "outcomes": outcomes,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


async def run_tick(
    open_bus: Callable[[], Any],
    handler: HandlerDeclaredCloneRefresh,
    host: str,
    wait_seconds: float,
) -> ModelDeclaredCloneRefreshReceipt:
    """Run the declared-clone refresh for this host's own row and return its receipt."""
    async with open_bus() as bus:
        return await run_declared_refresh(
            bus,
            handler,
            ModelDeclaredCloneRefreshRequest(hosts=(host,)),
            group_name=host,
            wait_seconds=wait_seconds,
        )


def tick(
    *,
    overlay: ModelDeclaredCloneTickOverlay,
    env: Mapping[str, str],
    state_dir: Path,
    open_bus: Callable[[], Any],
    handler: HandlerDeclaredCloneRefresh,
) -> int:
    """One host's tick: refresh its declared clone, print the receipt, write the evidence."""
    host = ""
    try:
        host = host_name(overlay, env)
        receipt = asyncio.run(run_tick(open_bus, handler, host, overlay.wait_seconds))
    except (ValueError, LabRunBusError, TimeoutError, NodeOverlayError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        write_evidence(state_dir, host, None, EXIT_INDETERMINATE)
        return EXIT_INDETERMINATE
    code = exit_code(receipt)
    write_evidence(state_dir, host, receipt, code)
    sys.stdout.write(receipt.model_dump_json() + "\n")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    deadline = load_node_timeouts(NODE).get("declared_clone_tick_seconds")
    if deadline:
        faulthandler.dump_traceback_later(float(deadline), exit=True)
    try:
        overlay = load_structured_overlay(NODE, ModelDeclaredCloneTickOverlay, env)
        host = host_name(overlay, env)
        topics = load_declared_clone_topics()
        table = resolve_lab_hosts_table()
        repo_dir = resolve_overlay_key(f"repos.{topics.repo_role}")
        handler = HandlerDeclaredCloneRefresh(
            table, repo_dir, TransportSubprocess(local_host_name=host)
        )
    except (NodeOverlayError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        write_evidence(args.state_dir, "", None, EXIT_INDETERMINATE)
        return EXIT_INDETERMINATE

    def open_bus() -> Any:
        return open_lab_run_bus(
            bus="inmemory" if overlay.backend == "inmemory" else "kafka",
            lane=overlay.bus_lane,
            kafka_bootstrap=None,
            **{_REGISTRY_KW: args.registry_root},
        )

    return tick(
        overlay=overlay,
        env=env,
        state_dir=args.state_dir,
        open_bus=open_bus,
        handler=handler,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["EXIT_INDETERMINATE", "EXIT_OK", "EXIT_REFUSED", "NODE", "main"]
