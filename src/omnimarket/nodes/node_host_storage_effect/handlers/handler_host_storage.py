# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host storage effect handler: ``handle(request) -> result`` (OMN-22906).

Runs one declared storage command on a host and reads the free bytes on the command's measured path
before and after. Anything the overlay does not declare is refused with a typed result before a
process starts; a command never runs when the free bytes before it cannot be read.
"""

from __future__ import annotations

from omnimarket.nodes.node_host_storage_effect.handlers.backend_in_memory import (
    BackendHostStorageInMemory,
)
from omnimarket.nodes.node_host_storage_effect.handlers.backend_subprocess import (
    BackendHostStorageSubprocess,
)
from omnimarket.nodes.node_host_storage_effect.handlers.protocols import (
    ProtocolHostStorageBackend,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageBackend,
    EnumHostStorageOutcome,
    EnumHostStorageRefusal,
    ModelHostStorageRequest,
    ModelHostStorageResult,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
)


def select_backend(overlay: ModelHostStorageOverlay) -> ProtocolHostStorageBackend:
    """The backend the overlay names."""
    if overlay.backend is EnumHostStorageBackend.IN_MEMORY:
        return BackendHostStorageInMemory(overlay)
    return BackendHostStorageSubprocess(
        overlay.free_bytes_argv, overlay.free_bytes_timeout_seconds
    )


class HandlerHostStorage:
    def __init__(
        self, overlay: ModelHostStorageOverlay, backend: ProtocolHostStorageBackend
    ) -> None:
        self._overlay = overlay
        self._backend = backend

    def handle(self, request: ModelHostStorageRequest) -> ModelHostStorageResult:
        host = self._overlay.host(request.host)
        if host is None:
            return self._refused(
                request,
                EnumHostStorageRefusal.HOST_UNKNOWN,
                "no row for this host in the overlay",
            )
        command = self._overlay.command(request.command)
        if command is None:
            return self._refused(
                request,
                EnumHostStorageRefusal.COMMAND_UNKNOWN,
                "the overlay declares no such command",
            )
        if command.name not in host.commands:
            return self._refused(
                request,
                EnumHostStorageRefusal.COMMAND_NOT_DECLARED_FOR_HOST,
                "the host's row does not list this command",
            )
        before = self._backend.free_bytes(host, command.measured_path)
        if before is None:
            return self._refused(
                request,
                EnumHostStorageRefusal.FREE_BYTES_UNREADABLE,
                "free bytes before the command could not be read; nothing ran",
            )
        run = self._backend.run(host, command)
        after = self._backend.free_bytes(host, command.measured_path)
        ok = run.exit_code == 0 and not run.timed_out
        detail = None
        if run.timed_out:
            detail = "timed out"
        elif not ok:
            detail = f"exit {run.exit_code}: {run.output_tail}".strip()
        return ModelHostStorageResult(
            host=request.host,
            command=request.command,
            outcome=(
                EnumHostStorageOutcome.OK if ok else EnumHostStorageOutcome.FAILED
            ),
            detail=detail,
            exit_code=run.exit_code,
            timed_out=run.timed_out,
            free_bytes_before=before,
            free_bytes_after=after,
        )

    @staticmethod
    def _refused(
        request: ModelHostStorageRequest,
        refusal: EnumHostStorageRefusal,
        detail: str,
    ) -> ModelHostStorageResult:
        return ModelHostStorageResult(
            host=request.host,
            command=request.command,
            outcome=EnumHostStorageOutcome.REFUSED,
            refusal=refusal,
            detail=detail,
        )
