# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed outcome of handing an observation to the durable spool."""

from pydantic import BaseModel, ConfigDict


class ModelPrStateEmitResult(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    accepted: bool
    digest: str
    event_type: str
    topic: str
    published: bool = False
    error: str | None = None
