# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Make one host call through the injected ProtocolLaneHostTransport (OMN-22848).

The handler owns no transport: the caller injects the ssh adapter for a real host
or the scripted adapter for a test. A handler built with none refuses to act.
It routes every operation of the enum to the transport method of the same name.
"""

from __future__ import annotations

from typing import Literal

from omnibase_spi.protocols.lane import ProtocolLaneHostTransport

from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
    REQUEST_FIELD,
    RESULT_FIELD,
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
        answer = getattr(transport, op.value)(getattr(request, REQUEST_FIELD[op]))
        return ModelRemoteLaneHostCallResult(operation=op, **{RESULT_FIELD[op]: answer})


__all__ = ["HandlerRemoteLaneStartEffect"]
