# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the delegation output extract compute (OMN-19600)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    MAX_OUTPUT_FILES,
    ModelDeclaredOutputs,
    ModelDelegationOutputFile,
    ModelDelegationOutputManifest,
)

#: Largest reply the compute will parse: every file at its cap, plus framing.
MAX_RESPONSE_TEXT_CHARS: int = 4 * 1_048_576


class ModelDelegationOutputExtractRequest(BaseModel):
    """An accepted delegation reply and the files the caller declared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    response_text: str = Field(..., max_length=MAX_RESPONSE_TEXT_CHARS)
    declared_outputs: ModelDeclaredOutputs


class ModelDelegationOutputExtractResult(BaseModel):
    """The accepted files, in declaration order, and the manifest naming them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ModelDelegationOutputFile, ...] = Field(
        default=(), max_length=MAX_OUTPUT_FILES
    )
    manifest: ModelDelegationOutputManifest


__all__ = [
    "MAX_RESPONSE_TEXT_CHARS",
    "ModelDelegationOutputExtractRequest",
    "ModelDelegationOutputExtractResult",
]
