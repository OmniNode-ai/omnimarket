# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start one lab lane: the node's handler over the orchestration core (OMN-22848).

``handle(request) -> result`` runs the whole sequence with the collaborators the handler was
built with. ``build_real`` wires the real backends from the overlay: the ssh transport, the
ledger append effect over the ledger writer command and the ledger file, the receipt file.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from omnibase_spi.protocols.lane import ProtocolLaneHostPool

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartRequest,
    ModelRemoteLaneStartResult,
    ModelRemoteLaneStartSettings,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.core import (
    FileReceiptSink,
    RemoteLaneStartOrchestrator,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)


class HandlerRemoteLaneStart:
    """Run the lane start sequence for one request."""

    def __init__(
        self,
        build: Callable[[ModelRemoteLaneStartRequest], RemoteLaneStartOrchestrator]
        | None = None,
    ) -> None:
        self._build = build

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    def handle(
        self, request: ModelRemoteLaneStartRequest
    ) -> ModelRemoteLaneStartResult:
        if self._build is None:
            raise RuntimeError(
                "remote-lane start handler has no collaborators; build it with build_real"
            )
        return self._build(request).run(request)


def build_real(
    settings: ModelRemoteLaneStartSettings,
    host_pool: ProtocolLaneHostPool,
    *,
    stop_requested: Callable[[], bool],
    sleep: Callable[[float], None],
    echo: Callable[[str], None] = print,
) -> HandlerRemoteLaneStart:
    """The real-backend wiring; every value comes from the overlay or the request."""

    def build(request: ModelRemoteLaneStartRequest) -> RemoteLaneStartOrchestrator:
        ledger_file = LocalLedgerFile(Path(request.ledger_path))
        ledger = HandlerWorkLedgerAppendEffect(
            runner=LocalLedgerAppendCommand(
                [*settings.ledger_append_argv, request.ledger_path]
            ),
            reader=ledger_file,
            writer_principals=[settings.ledger_writer_principal],
        )
        transport = SshLaneHostTransport(
            ssh_argv=settings.ssh_argv,
            host_run_root=settings.host_run_root,
            call_timeout_seconds=settings.host_call_timeout_seconds,
            detach_argv=settings.detach_argv,
        )
        return RemoteLaneStartOrchestrator(
            settings=settings,
            host_pool=host_pool,
            ledger=ledger,
            ledger_reader=ledger_file,
            host=HandlerRemoteLaneStartEffect(transport),
            receipts=FileReceiptSink(settings.receipt_dir),
            echo=echo,
            sleep=sleep,
            stop_requested=stop_requested,
            requesting_host=socket.gethostname().split(".", 1)[0],
        )

    return HandlerRemoteLaneStart(build)


__all__ = ["HandlerRemoteLaneStart", "build_real"]
