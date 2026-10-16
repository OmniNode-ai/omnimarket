# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The attachable answer verdict includes its source run directory."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_answer_check_outcome import (
    EnumAnswerCheckOutcome,
)


class ModelAnswerCheckVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: EnumAnswerCheckOutcome
    run_directory: str
    final_block: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
