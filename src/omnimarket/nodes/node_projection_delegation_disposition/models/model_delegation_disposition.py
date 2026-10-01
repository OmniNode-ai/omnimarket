# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a disposition request, row and fold result."""

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.delegation_disposition import ModelDelegationDispositionRecorded
from omnimarket.projection.envelope import strip_runner_injected_keys


class ModelDelegationDispositionProjectionRequest(ModelDelegationDispositionRecorded):
    """Strip runtime metadata; producer time remains required."""

    @model_validator(mode="before")
    @classmethod
    def _accept_runtime_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return strip_runner_injected_keys(data)
        return data


class ModelDelegationDispositionRow(ModelDelegationDispositionRecorded):
    """One current disposition per tenant and delegation correlation."""


class ModelDelegationDispositionProjectionResult(BaseModel):
    """Purely derived rows for the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelDelegationDispositionRow, ...]
