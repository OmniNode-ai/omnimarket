# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule declaration for the tenant outbox flush."""

from __future__ import annotations

from pydantic import ConfigDict

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelTenantOutboxFlushScheduleOverlay(ModelScheduleDeclaration):
    """The cadence of the outbox flush; the store and the bus come from the flush node's own overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")


__all__: list[str] = ["ModelTenantOutboxFlushScheduleOverlay"]
