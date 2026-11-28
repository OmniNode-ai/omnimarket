# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-agent harness's local rung: the plain local rung, pinned to the local tier's least-busy model, which the port never leaves (OMN-19998).

The local tier's model is picked by the routing authority (``backend_id_for_tier`` with the live load
snapshot, fewest waiting requests first) and the call is run by ``LocalDelegationDispatchPort``, the code
the plain local rung already runs. No routing or call code is repeated here.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from omnimarket.handlers.routing.spread_load_snapshot import read_spread_load_snapshot
from omnimarket.models.model_harness_backend_call import ModelHarnessRunReceipt

_LOCAL_TIER = "local"
_QUALITY_MODE = "extend_task_class"
_TERMINAL_MARGIN_SECONDS = 5


def _count(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


class HandlerLabAgentLocalRung:
    """``ProtocolLocalRungDispatch`` over the local dispatch port."""

    def __init__(self, port: Any = None) -> None:
        self._port = port

    def _bound_port(self) -> Any:
        if self._port is None:
            from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
                select_delegation_dispatch_port,
            )

            self._port = select_delegation_dispatch_port(None)
        return self._port

    async def dispatch(
        self, *, prompt: str, task_type: str, timeout_seconds: float
    ) -> ModelHarnessRunReceipt:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            backend_id_for_tier,
        )

        started = time.monotonic()
        correlation_id = uuid4()
        backend_id = backend_id_for_tier(
            _LOCAL_TIER,
            task_type,
            spread_key=str(correlation_id),
            prompt=prompt,
            load_state=read_spread_load_snapshot(),
        )
        if backend_id is None:
            return ModelHarnessRunReceipt(
                wall_seconds=time.monotonic() - started,
                exit_cause="launch_failed",
                detail=f"the local tier declares no model for {task_type!r}",
            )
        result = await self._bound_port().dispatch(
            prompt=prompt,
            task_type=task_type,
            correlation_id=correlation_id,
            max_tokens=None,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=max(1, int(timeout_seconds)),
            terminal_delivery_margin_seconds=_TERMINAL_MARGIN_SECONDS,
            quality_contract_mode=_QUALITY_MODE,
            acceptance_criteria=(),
            tenant_id=None,
            backend_id=backend_id,
        )
        wall = time.monotonic() - started
        answer = str(result.get("content") or "")
        if result.get("status") != "completed" or not answer.strip():
            return ModelHarnessRunReceipt(
                wall_seconds=wall,
                exit_cause="turn_failed",
                detail=str(result.get("error_message") or "")[:300],
            )
        model = result.get("model_name")
        return ModelHarnessRunReceipt(
            answer=answer,
            tokens_in=_count(result.get("input_tokens")),
            tokens_out=_count(result.get("output_tokens")),
            wall_seconds=wall,
            exit_cause="completed",
            model=str(model) if model else None,
        )


__all__: list[str] = ["HandlerLabAgentLocalRung"]
