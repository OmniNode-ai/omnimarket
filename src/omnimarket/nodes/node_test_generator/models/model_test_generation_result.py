# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTestGenerationResult — output of node_test_generator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTestGenerationResult(BaseModel):
    """Result of deterministic test generation from a ticket contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_source: str = Field(
        ...,
        description="Generated Python test source code.",
    )
    test_hash: str = Field(
        ...,
        description="SHA256 hex digest of the generated test_source (determinism anchor).",
    )
    contract_hash: str = Field(
        ...,
        description="SHA256 hex digest of the serialized input contract.",
    )
    generator_version: str = Field(
        ...,
        description="Version of the generator that produced this result.",
    )
    template_hash: str = Field(
        ...,
        description="SHA256 hex digest of the template(s) used for generation.",
    )
    generation_profile_hash: str = Field(
        ...,
        description="Hash or label of the generation profile configuration.",
    )
    generated_at: str = Field(
        ...,
        description="ISO8601 UTC timestamp of when this result was produced.",
    )


__all__: list[str] = ["ModelTestGenerationResult"]
