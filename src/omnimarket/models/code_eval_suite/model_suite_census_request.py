# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census request: the suite's tasks and their pre-control exclusions (OMN-23883)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.code_eval_suite.model_suite_task import ModelCodeEvalSuiteTask


class ModelCodeEvalSuiteExclusion(BaseModel):
    """A candidate dropped before its control ran, with the reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1)
    count: int = Field(ge=1)


class ModelCodeEvalSuiteCensusRequest(BaseModel):
    """The verified-or-not task list the census answers about.

    An empty ``tasks`` list is a valid request that ends as the typed
    ``no_tasks`` failure, so the field carries no minimum length.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: str = Field(min_length=1)
    tasks: tuple[ModelCodeEvalSuiteTask, ...]
    pre_control_exclusions: tuple[ModelCodeEvalSuiteExclusion, ...] = ()


__all__ = [
    "ModelCodeEvalSuiteCensusRequest",
    "ModelCodeEvalSuiteExclusion",
]
