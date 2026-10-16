# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire mirror of the Slack publish command the alert is delivered as."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelKeyPoolExpirySlackCommand(BaseModel):
    """Field-for-field mirror of ``ModelSlackPublish`` (onex.cmd.omnimarket.slack-publish.v1).

    Mirrored, not imported, because one node does not import another node's
    model package; the wire shape is the contract. A test round-trips this
    payload through the real publish model so the two cannot drift.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    correlation_id: UUID


__all__ = ["ModelKeyPoolExpirySlackCommand"]
