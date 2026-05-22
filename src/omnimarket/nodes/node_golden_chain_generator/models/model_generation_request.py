# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for golden chain generation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelGoldenChainGenerationRequest"]


class ModelGoldenChainGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_yaml: str
    contract_hash: str
    generator_version: str
    test_source: str = ""
    test_hash: str = ""
    template_hash: str = ""
    generation_profile_hash: str = ""
    node_metadata: dict[str, object] = Field(default_factory=dict)
