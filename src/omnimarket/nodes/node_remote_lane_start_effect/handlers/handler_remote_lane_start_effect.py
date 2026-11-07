# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Make one host call through the injected ProtocolLaneHostTransport (OMN-22848).

The handler owns no transport: the caller injects the ssh adapter for a real host
or the scripted adapter for a test. A handler built with none refuses to act.
"""

from __future__ import annotations

from typing import Literal

from omnibase_spi.protocols.lane import ProtocolLaneHostTransport

from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
    EnumLaneHostOperation,
    ModelRemoteLaneHostCall,
    ModelRemoteLaneHostCallResult,
)


class HandlerRemoteLaneStartEffect:
    """Route one host call to the transport and wrap its spi result."""

    def __init__(self, transport: ProtocolLaneHostTransport | None = None) -> None:
        self._transport = transport

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelRemoteLaneHostCall) -> ModelRemoteLaneHostCallResult:
        if self._transport is None:
            raise RuntimeError("remote-lane start effect has no host transport")
        transport = self._transport
        op = request.operation
        if op is EnumLaneHostOperation.PREPARE_WORKTREE and request.prepare:
            return ModelRemoteLaneHostCallResult(
                operation=op, prepare=transport.prepare_worktree(request.prepare)
            )
        if op is EnumLaneHostOperation.START_ENGINE_DETACHED and request.start:
            return ModelRemoteLaneHostCallResult(
                operation=op, start=transport.start_engine_detached(request.start)
            )
        if op is EnumLaneHostOperation.READ_ENGINE_EXIT and request.engine_exit:
            return ModelRemoteLaneHostCallResult(
                operation=op,
                engine_exit=transport.read_engine_exit(request.engine_exit),
            )
        if op is EnumLaneHostOperation.STOP_ENGINE and request.stop:
            return ModelRemoteLaneHostCallResult(
                operation=op, stop=transport.stop_engine(request.stop)
            )
        if op is EnumLaneHostOperation.READ_LOG_TAIL and request.log_tail:
            return ModelRemoteLaneHostCallResult(
                operation=op, log_tail=transport.read_log_tail(request.log_tail)
            )
        raise ValueError(
            f"host call {op.value} carries no request"
        )  # unreachable: the model refuses it


__all__ = ["HandlerRemoteLaneStartEffect"]
