# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex bus-binding``: read the binding state a tick prints, and replay the ledger outbox (OMN-20275)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, bound_lane_principal
from omnimarket.handlers.bus_binding import outbox
from omnimarket.handlers.bus_binding.select import (
    default_state_dir,
    open_bound_bus,
    read_state,
    replay_outbox,
)
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.bus_binding import EnumBusBinding, EnumBusBindingState
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller


@click.group("bus-binding")
@click.pass_context
def bus_binding_group(ctx: click.Context) -> None:
    """The bus binding a command used, and the outbox it holds while degraded."""
    ctx.ensure_object(dict)


@bus_binding_group.command("status")
@click.option("--lane", required=True)
def status_command(lane: str) -> None:
    """Print the tick line and the STATUS cell for the lane's last binding decision."""
    state_dir = default_state_dir()
    state = read_state(state_dir, lane)
    binding = (
        EnumBusBinding.INMEMORY
        if state is EnumBusBindingState.DEGRADED
        else EnumBusBinding.DURABLE
    )
    pending = outbox.pending_count(state_dir)
    click.echo(
        f"bus-binding lane={lane} binding={binding.value} state={state.value} pending_outbox={pending}"
    )
    click.echo(f"status_cell: bus_binding={binding.value} bus_state={state.value}")


@bus_binding_group.command("replay")
@_with_bus_options
def replay_command(
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> None:
    """Replay the ledger outbox to the durable bus; exits 75 while the bus is still degraded."""
    state_dir = default_state_dir()

    async def main() -> int:
        async with open_bound_bus(
            bus="inmemory" if bus == "inmemory" else "kafka",
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            workspace_root=omnibase_path,
            state_dir=state_dir,
            pending_outbox=lambda: outbox.pending_count(state_dir),
        ) as bound:
            if (
                bound.decision.degraded
                or bound.decision.binding is EnumBusBinding.INMEMORY
            ):
                click.echo(
                    json.dumps(
                        {
                            "replayed": 0,
                            "remaining": outbox.pending_count(state_dir),
                            "binding": "inmemory",
                        }
                    )
                )
                return 75
            caller = WorkLedgerAppendCaller(bound.bus, principal=bound_lane_principal())
            try:
                result = await replay_outbox(
                    bound,
                    state_dir,
                    lambda queued: caller.append(queued, timeout_s=60.0),
                )
            finally:
                await caller.stop()
        click.echo(json.dumps(result.__dict__, sort_keys=True))
        return 0 if result.stopped is None else 75

    try:
        code = asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        code = 69
    sys.exit(code)
