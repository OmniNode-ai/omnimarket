# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Concrete terminal event classes for contract-derived bus routing."""

from __future__ import annotations

from typing import Literal

from omnibase_core.enums.enum_model_swap_validation_status import (
    EnumModelSwapValidationStatus,
)
from omnibase_core.models.model_swap_validation import ModelModelSwapValidationResult


class ModelModelSwapValidationCompleted(ModelModelSwapValidationResult):
    """A completed evidence collection, including reject and abstain verdicts."""

    status: Literal[EnumModelSwapValidationStatus.COMPLETED] = (
        EnumModelSwapValidationStatus.COMPLETED
    )


class ModelModelSwapValidationFailed(ModelModelSwapValidationResult):
    """A terminal refusal or failure, never a routing instruction."""

    status: Literal[EnumModelSwapValidationStatus.FAILED] = (
        EnumModelSwapValidationStatus.FAILED
    )


def terminal_event(
    result: ModelModelSwapValidationResult,
) -> ModelModelSwapValidationCompleted | ModelModelSwapValidationFailed:
    """Keep the platform evidence unchanged while selecting its terminal class."""
    model = (
        ModelModelSwapValidationCompleted
        if result.status == EnumModelSwapValidationStatus.COMPLETED
        else ModelModelSwapValidationFailed
    )
    return model.model_validate(result.model_dump())
