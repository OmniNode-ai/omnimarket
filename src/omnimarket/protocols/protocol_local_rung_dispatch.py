# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plain local rung, as the lab-agent harness reaches it (OMN-19998)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_harness_backend_call import ModelHarnessRunReceipt


@runtime_checkable
class ProtocolLocalRungDispatch(Protocol):
    """Runs one prompt on the local tier's least-busy model, through the local rung code path."""

    async def dispatch(
        self, *, prompt: str, task_type: str, timeout_seconds: float
    ) -> ModelHarnessRunReceipt: ...


__all__: list[str] = ["ProtocolLocalRungDispatch"]
