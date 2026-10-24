# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bus publication attempt, including unsuccessful delivery."""

from pydantic import BaseModel, ConfigDict


class ModelCaptureWatchDelivery(BaseModel):
    """Published means accepted by the bus; Slack owns final delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str
    actor: str | None
    command_topic: str
    idempotency_key: str
    published: bool
    error: str | None = None
