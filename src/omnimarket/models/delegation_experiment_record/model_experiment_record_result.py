# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The experiments whose record passed and the refusals of the rest."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_experiment_record.model_experiment_issue import (
    ModelExperimentIssue,
)
from omnimarket.models.delegation_experiment_record.model_experiment_run_count import (
    ModelExperimentRunCount,
)


class ModelExperimentRecordResult(BaseModel):
    """A record is accepted whole or has issues; one bad record never hides a good one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted_experiment_ids: tuple[str, ...] = ()
    run_counts: tuple[ModelExperimentRunCount, ...] = ()
    issues: tuple[ModelExperimentIssue, ...] = ()
