# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The required contexts still running on the head, sorted."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelPendingRequiredVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pending: tuple[str, ...]


__all__: list[str] = ["ModelPendingRequiredVerdict"]
