# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Types of the omnibase_spi source-control adapter (OMN-20912).

The adapter, ``HandlerSourceControlGithub``, answers what the shared GitHub
transport can honestly serve and refuses the rest with
:class:`SourceControlOperationNotSupportedError`, which names the operation and
the reason. A write the write effect refused, or ran in dry_run, raises
:class:`SourceControlWriteRefusedError` carrying the typed result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from omnibase_spi.protocols.types.protocol_base_types import (
    ContextValue,
    LiteralHealthStatus,
)

from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
)


class SourceControlOperationNotSupportedError(NotImplementedError):
    """The adapter does not serve this protocol operation, and says why."""

    def __init__(self, operation: str, reason: str) -> None:
        super().__init__(f"{operation} is not served by this adapter: {reason}")
        self.operation = operation
        self.reason = reason


class SourceControlWriteRefusedError(RuntimeError):
    """The write effect refused the write, or ran it in dry_run."""

    def __init__(self, result: ModelRepoWriteCompleted | ModelRepoWriteFailed) -> None:
        if isinstance(result, ModelRepoWriteFailed):
            message = f"{result.operation.value} refused: {result.reason.value}: {result.detail}"
        else:
            message = (
                f"{result.operation.value} ran in {result.mode.value} for "
                f"{result.repo}; nothing was written"
            )
        super().__init__(message)
        self.result = result


@dataclass
class ModelSourceControlHealthStatus:
    """Satisfies omnibase_spi ``ProtocolServiceHealthStatus``.

    A plain (non-frozen) dataclass because the protocol declares settable
    attributes and a ``ContextValue`` is a protocol, not a schema type.
    """

    service_id: UUID
    status: LiteralHealthStatus
    last_check: datetime
    details: dict[str, ContextValue] = field(default_factory=dict)

    async def validate_health_status(self) -> bool:
        return self.last_check is not None

    def is_healthy(self) -> bool:
        return self.status == "healthy"


__all__: list[str] = [
    "ModelSourceControlHealthStatus",
    "SourceControlOperationNotSupportedError",
    "SourceControlWriteRefusedError",
]
