# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic projection input and labelled-item row."""

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.delegation_eval import (
    ModelDelegationEvalItemLabelled,
)


class ModelDelegationEvalProjectionRequest(ModelDelegationEvalItemLabelled):
    """Accept runtime metadata and producer time; never invent an ingest time."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _accept_envelope_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and "observed_at" not in data:
            return {**data, "observed_at": data.get("_envelope_timestamp")}
        return data


class ModelDelegationEvalRow(ModelDelegationEvalItemLabelled):
    """One (tenant, item, rater, rubric) label in the lab table."""

    item_key: str


class ModelDelegationEvalProjectionResult(BaseModel):
    """Rows derived by the pure fold, to be persisted by the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelDelegationEvalRow, ...]
