# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire record of the class rubric compute on one delegation attempt."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelAttemptRubricVerdict(BaseModel):
    """Per-attempt record of node_delegation_rubric_check_compute (OMN-20165).

    Recorded only: no accept, refuse, retry or escalation decision reads it
    until OMN-20166 turns a measured class on. ``undetermined_criteria`` names
    every criterion that could not decide, so an UNDETERMINED outcome is never
    read as a pass. ``rubric_check_error`` there means the compute itself could
    not run on this attempt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rubric_version: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    outcome: Literal["PASS", "FAIL", "UNDETERMINED"]
    failed_criteria: tuple[str, ...] = ()
    undetermined_criteria: tuple[str, ...] = ()
