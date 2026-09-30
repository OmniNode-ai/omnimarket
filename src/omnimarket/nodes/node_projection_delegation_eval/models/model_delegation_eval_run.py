# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run-completed projection input and its two row kinds (OMN-19793, EV.4)."""

from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from omnimarket.events.delegation_eval import (
    ModelDelegationEvalItemVerdict,
    ModelDelegationEvalResultRow,
    ModelDelegationEvalRunCompleted,
)


class ModelDelegationEvalRunProjectionRequest(ModelDelegationEvalRunCompleted):
    """Accept runtime metadata and producer time; never invent an ingest time."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _accept_envelope_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and "observed_at" not in data:
            return {**data, "observed_at": data.get("_envelope_timestamp")}
        return data


class _RunKeyed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    eval_run_id: UUID
    manifest_id: str
    gate_version: str
    rater_role: str
    rubric_version: str
    observed_at: AwareDatetime


class ModelDelegationEvalItemVerdictRow(_RunKeyed, ModelDelegationEvalItemVerdict):
    """One row of delegation_eval_item_verdicts: (tenant, run, item)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDelegationEvalResultsRow(_RunKeyed, ModelDelegationEvalResultRow):
    """One row of delegation_eval_results: (tenant, run, class, stratum, arm)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label_set_sha256: str


class ModelDelegationEvalRunProjectionResult(BaseModel):
    """Rows derived by the pure fold, to be persisted by the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    verdict_rows: tuple[ModelDelegationEvalItemVerdictRow, ...]
    result_rows: tuple[ModelDelegationEvalResultsRow, ...]
