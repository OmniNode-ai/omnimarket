# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A check skipped for a recorded reason."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelGateCheckSkip(BaseModel):
    """A check skipped for a recorded reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    reason: str
