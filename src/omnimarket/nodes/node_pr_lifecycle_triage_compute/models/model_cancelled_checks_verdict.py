# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The names whose newest check copy on the head is completed and cancelled."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCancelledChecksVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cancelled: tuple[str, ...]


__all__: list[str] = ["ModelCancelledChecksVerdict"]
