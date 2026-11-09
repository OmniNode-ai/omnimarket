# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports of join J6: the durable audit rows and their projection (rebuild plan section 4.1)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_gate_store_join_j6 import ModelJoinJ6AuditRow


@runtime_checkable
class ProtocolJoinJ6AuditSink(Protocol):
    """The durable audit rows of stops; one row per row id."""

    def append_if_absent(self, row: ModelJoinJ6AuditRow) -> bool:
        """Append the row unless its id is already there; True when this call wrote it."""
        ...

    def read(self, row_id: str) -> ModelJoinJ6AuditRow | None:
        """The durable row with this id, read back from the sink."""
        ...


@runtime_checkable
class ProtocolJoinJ6Projection(Protocol):
    """The projection of the audit rows, read by the floor and by operators."""

    def project(self, row: ModelJoinJ6AuditRow) -> None:
        """Project one durable row."""
        ...

    def read(self, row_id: str) -> ModelJoinJ6AuditRow | None:
        """The projected row with this id, or None when the projection never heard it."""
        ...
