# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The attachable documentation draw record includes its source run directory."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_doc_check_outcome import (
    EnumDocCheckOutcome,
)


class ModelDocCheckVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: EnumDocCheckOutcome
    run_directory: str
    failures: tuple[str, ...] = ()
