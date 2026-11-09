# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start the lane runner detached, or run one bounded command, through the runner the container provides.

The effect refuses a spec whose argv names the script runner, for either operation: the runner it starts
is the node entry of the lane start, never the script. It decides nothing else: the argv, environment and
working directory arrive on the request, built by the orchestrator from its overlay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omnibase_spi.protocols.lane import ProtocolLaneProcessRunner

from omnimarket.nodes.node_lane_process_effect.models import (
    EnumLaneProcessOperation,
    EnumLaneProcessOutcome,
    ModelLaneProcessRequest,
    ModelLaneProcessResult,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

SCRIPT_RUNNER_NAME = "onex_remote_lane"
# A Protocol is the container's service key; it is never instantiated here.
RUNNER_PROTOCOL: type[Any] = ProtocolLaneProcessRunner


class HandlerLaneProcess:
    """Run one operation of ProtocolLaneProcessRunner and answer its typed outcome.

    The handler takes the container (the one constructor the runtime resolver and the handler-DI gate accept) and
    resolves the runner at the effect boundary, inside ``handle``: a wiring gap fails loud when an operation runs.
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    def handle(self, request: ModelLaneProcessRequest) -> ModelLaneProcessResult:
        operation = request.operation
        named = next(
            (arg for arg in request.spec.argv if SCRIPT_RUNNER_NAME in arg), None
        )
        if named is not None:
            return ModelLaneProcessResult(
                operation=operation,
                outcome=EnumLaneProcessOutcome.REFUSED,
                cause=f"argv names the script runner: {named}",
            )
        runner: ProtocolLaneProcessRunner = self._container.get_service(RUNNER_PROTOCOL)
        if operation is EnumLaneProcessOperation.START_DETACHED:
            started = runner.start_detached(request.spec)
            return ModelLaneProcessResult(
                operation=operation,
                outcome=EnumLaneProcessOutcome(started.state),
                pid=started.pid,
                cause=started.cause,
            )
        if request.timeout_seconds is None:
            return ModelLaneProcessResult(
                operation=operation,
                outcome=EnumLaneProcessOutcome.FAILED,
                cause="run_bounded requires timeout_seconds",
            )
        ran = runner.run_bounded(request.spec, request.timeout_seconds)
        return ModelLaneProcessResult(
            operation=operation,
            outcome=EnumLaneProcessOutcome(ran.state),
            exit_code=ran.exit_code,
            stdout=ran.stdout,
            cause=ran.cause,
        )
