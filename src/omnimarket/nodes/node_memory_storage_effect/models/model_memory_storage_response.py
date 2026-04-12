# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Storage Response Model.

This module defines the response envelope for memory storage CRUD operations.
The response wraps operation results including retrieved snapshots, lists of
snapshot IDs, and error information when operations fail.

The model uses ModelMemorySnapshot from omnibase_core for the actual memory
data, keeping this module focused on the operation envelope pattern.

Example:
    >>> from omnimemory.nodes.node_memory_storage_effect.models import (
    ...     ModelMemoryStorageResponse
    ... )
    >>> response = ModelMemoryStorageResponse(
    ...     status="success",
    ...     snapshot=retrieved_snapshot
    ... )
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnibase_core.models.omnimemory import ModelMemorySnapshot

__all__ = ["ModelMemoryStorageResponse"]


class ModelMemoryStorageResponse(BaseModel):
    """Response envelope for memory storage operations.

    This model provides a consistent response structure for all memory storage
    operations (store, retrieve, delete, update, list). The status field indicates
    the operation outcome, while optional fields carry operation-specific results.

    Attributes:
        status: Operation status (success, error, not_found, permission_denied).
        snapshot: The retrieved or stored memory snapshot (for retrieve/store/update).
        snapshot_ids: List of snapshot identifiers (for list operations).
        error_message: Detailed error information when status is "error".
    """

    model_config = ConfigDict(strict=True)

    status: Literal["success", "error", "not_found", "permission_denied"] = Field(
        ...,
        description="Operation status: success, error, not_found, permission_denied",
    )
    snapshot: ModelMemorySnapshot | None = Field(
        default=None,
        description="Retrieved/stored snapshot",
    )
    snapshot_ids: list[str] | None = Field(
        default=None,
        description="List of snapshot IDs (for list operation)",
    )
    error_message: str | None = Field(
        default=None,
        description="Error details if status is error",
    )
