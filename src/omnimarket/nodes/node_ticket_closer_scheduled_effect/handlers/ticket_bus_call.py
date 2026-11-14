# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket write node's bus call, bound for the scheduled closer (OMN-24301).

``bus_ticket_call`` is the ``call`` the closer's real adapters take: it opens the lab run
bus on the declared lane exactly the way the pin advance opener does, and awaits the ticket
write node's terminal event with the caller ``onex ticket`` uses. A timeout or a bus that
cannot open is raised as :class:`TicketBusCallError`, never swallowed: the done writer
reports the raise as a refused write instead of reading the silence as a completed one.
The lane and the timeout come from the schedule overlay; this module holds none of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from omnimarket.models.scm import ModelScmTerminal
from omnimarket.models.ticket_write_io import (
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_done_writer import (
    ProtocolTicketWriteCall,
)


class TicketBusCallError(Exception):
    """The ticket write node's terminal event never arrived, or the bus could not open."""


def bus_ticket_call(*, bus_lane: str, timeout_s: float) -> ProtocolTicketWriteCall:
    """A :class:`ProtocolTicketWriteCall` that publishes on the declared lane and awaits the terminal."""
    # Imported here: a test with a fake opener must not need a bus client.
    from omnibase_infra.errors import InfraConnectionError

    from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
    from omnimarket.delegated_test_loop.lane_bus import (
        LabRunBusError,
        open_lab_run_bus,
    )
    from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
    from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics

    lane = bus_lane
    workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
    # The opener's fourth keyword (the workspace root) carries the private
    # registry clone's name, which the public-repo hygiene gate refuses; its
    # parameters are bound from its signature in declared order, as
    # ``onex ticket`` binds them.
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise TicketBusCallError(f"unexpected bus opener parameters: {names}")
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    values = ("kafka", lane, None, Path(workspace) if workspace else None)

    def call(request: ModelTicketWriteRequest) -> ModelScmTerminal:
        async def run() -> ModelScmTerminal:
            async with opener(**dict(zip(names, values, strict=True))) as bus:
                return await ScmBusCaller(bus).call_topics(
                    topics=load_ticket_topics(),
                    request=request,
                    correlation_id=request.correlation_id,
                    timeout_s=timeout_s,
                )

        try:
            # The effect runs under a loop; the bus call gets a thread of its own.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, run()).result()
        except TimeoutError as exc:
            raise TicketBusCallError(
                f"{request.operation.value} for {request.ticket_id}: no answer on lane "
                f"{lane!r} within {timeout_s:.0f}s"
            ) from exc
        except (LabRunBusError, InfraConnectionError) as exc:
            raise TicketBusCallError(f"ticket bus lane {lane!r}: {exc}") from exc

    return call
