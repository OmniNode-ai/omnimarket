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

from omnimarket.models.engine_login import (
    ModelEngineLoginPool,
)
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
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    local_serve_handler,
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
        # The check-then-append step runs under a flock beside the ledger, so two leaders
        # (processes) for one lane and attempt cannot both be told ACCEPTED for the CLAIM.
        ledger = local_serve_handler(
            LocalLedgerAppendCommand(
                [*settings.ledger_append_argv, request.ledger_path],
                write_via=settings.ledger_write_via,
            ),
            Path(request.ledger_path),
            socket.gethostname().split(".", 1)[0],
            [settings.ledger_writer_principal],
            None,
        )
        transport = SshLaneHostTransport(
            ssh_argv=settings.ssh_argv,
            host_run_root=settings.host_run_root,
            call_timeout_seconds=settings.host_call_timeout_seconds,
            detach_argv=settings.detach_argv,
            path_prefix=settings.host_path_prefix,
            registry_root_candidates=settings.registry_root_candidates,
            registry_marker=settings.registry_marker,
            login_pool=ModelEngineLoginPool(
                module=settings.login_pool_module,
                python_relative=settings.login_pool_python_relative,
                roots=settings.registry_root_candidates,
                draw_timeout_seconds=settings.login_draw_timeout_seconds,
            ),
        )
        return RemoteLaneStartOrchestrator(
            settings=settings,
            host_pool=host_pool,
            ledger=ledger,
            ledger_reader=ledger_file,
            host=HandlerRemoteLaneStartEffect(transport),
            login=transport,
            receipts=FileReceiptSink(settings.receipt_dir),
            echo=echo,
            sleep=sleep,
            stop_requested=stop_requested,
            requesting_host=socket.gethostname().split(".", 1)[0],
        )

    return HandlerRemoteLaneStart(build)


__all__ = ["HandlerRemoteLaneStart", "build_real"]
