# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry record for a previously-generated tool (OMN-13356).

This is the immutable record the registry returns. Match confidence and the
match reason are NOT fields here — they are computed per-request and attached to
``ModelToolReuseCandidate`` so the registry record stays a pure fact.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelGeneratedToolRecord(BaseModel):
    """Immutable registry record for one generated tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: str = Field(min_length=1, description="Stable unique tool identifier")
    tool_name: str = Field(
        min_length=1, description="ONEX node name (e.g. node_generated_xyz)"
    )
    handler_module: str = Field(
        min_length=1, description="Python module path to the handler"
    )
    handler_class: str = Field(min_length=1, description="Handler class name")
    contract_hash: str = Field(
        min_length=1, description="sha256:<hex> of the tool contract"
    )
    semantic_description: str = Field(
        description="Tool description extracted from the generated contract"
    )
    input_model_name: str = Field(min_length=1)
    output_model_name: str = Field(min_length=1)
    input_fields_hash: str = Field(
        min_length=1, description="sha256:<hex> of input model fields"
    )
    output_fields_hash: str = Field(
        min_length=1, description="sha256:<hex> of output model fields"
    )
    generated_at: datetime = Field(description="When this tool was generated")
    is_active: bool = Field(
        default=True, description="False once the tool is deprecated"
    )


__all__ = ["ModelGeneratedToolRecord"]
