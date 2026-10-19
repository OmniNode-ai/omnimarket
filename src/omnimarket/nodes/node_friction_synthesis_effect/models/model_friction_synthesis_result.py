# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed terminal event for friction accounting."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelFrictionSynthesisResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    exit_code: Literal[0, 1]
    stderr: str = ""
