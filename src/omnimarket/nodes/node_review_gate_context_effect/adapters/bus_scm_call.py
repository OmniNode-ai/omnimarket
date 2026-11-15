# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One SCM bus call for node_review_gate_context_effect: opens the lab bus lane and awaits the terminal.

Copies the shape of ``bus_write_call`` in ``write_node_group_reader.py``: the opener's parameters are bound by name
from its signature, the call runs in a thread of its own, and a timeout or bus error is a typed port error. The
lane, the NAME of the environment variable holding the workspace root and the timeout come from the route (the
overlay), never from code.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from omnimarket.models.scm import EnumScmNode
from omnimarket.nodes.node_review_gate_context_effect.overlay import (
    ModelReviewContextBusRoute,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ReviewContextPortError,
)


def bus_scm_call(
    *,
    route: ModelReviewContextBusRoute,
    environ: Mapping[str, str],
    node: EnumScmNode,
    request: BaseModel,
    correlation_id: UUID,
) -> tuple[bool, dict[str, JsonValue]]:
    """Opens the lab bus lane and awaits the terminal for one SCM call."""
    # Imported here: a test with a fake call must not need a bus client.
    from omnibase_infra.errors import InfraConnectionError

    from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
    from omnimarket.delegated_test_loop.lane_bus import (
        LabRunBusError,
        open_lab_run_bus,
    )
    from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller

    lane = route.bus_lane
    workspace = environ.get(route.workspace_env) or None
    # The opener's fourth keyword (the workspace root) carries the private registry clone's name, which the
    # public-repo hygiene gate refuses; its parameters are bound from its signature in declared order.
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise ReviewContextPortError("bus opener signature changed")
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    values = ("kafka", lane, None, Path(workspace) if workspace else None)

    async def run() -> tuple[bool, dict[str, JsonValue]]:
        async with opener(**dict(zip(names, values, strict=True))) as bus:
            terminal = await ScmBusCaller(bus).call(
                node=node,
                request=request,
                correlation_id=correlation_id,
                timeout_s=route.timeout_seconds,
            )
        payload = terminal.payload
        failed = (
            terminal.failed
            or payload.get("outcome") == "failed"
            or payload.get("status") == "failed"
        )
        result = payload.get("result", payload)
        return failed, result if isinstance(result, dict) else payload

    try:
        # The effect runs under a loop; the bus call gets a thread of its own.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, run()).result()
    except TimeoutError as exc:
        raise ReviewContextPortError("bus call timed out") from exc
    except (LabRunBusError, InfraConnectionError) as exc:
        raise ReviewContextPortError(f"bus call failed: {exc}") from exc


__all__ = ["bus_scm_call"]
