# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the plan owner check."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.plan_owner_check import (
    ModelPlanOwnerEvent,
)


class ProtocolPlanOwnerEventSink(Protocol):
    def emit(self, event: ModelPlanOwnerEvent) -> None: ...


__all__ = ["ProtocolPlanOwnerEventSink"]
