# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus publications made for one finding, including unsuccessful ones."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelWorkProgressDelivery(BaseModel):
    """Published means accepted by the bus; Slack owns final delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    stalled_published: bool
    command_published: bool
    error: str | None = None
