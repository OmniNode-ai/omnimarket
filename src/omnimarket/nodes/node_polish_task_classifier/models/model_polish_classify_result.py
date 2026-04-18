# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass


class ModelPolishClassifyResult(BaseModel):
    """Decision table output: exactly one task class per invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: EnumPolishTaskClass
    confidence: float
    reason: str
