# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable, closed model base for fleet boundaries."""

from pydantic import BaseModel, ConfigDict


class ModelFrozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
