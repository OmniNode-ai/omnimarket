# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of generating a node's golden and error chain tests (OMN-19998)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelScenarioTestGenerationRequest(BaseModel):
    """A node's whole contract document, read as data, and where its test imports from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(..., min_length=1)
    contract_document: dict[str, JsonValue]
    generator_version: str = Field(default="1.0.0", min_length=1)


class ModelScenarioTestGenerationResult(BaseModel):
    """The generated test module, or the issues that refused it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok", "refused"]
    issues: tuple[str, ...] = ()
    case_names: tuple[str, ...] = ()
    break_pairs: int = 0
    test_source: str = ""
    test_hash: str = ""


__all__ = ["ModelScenarioTestGenerationRequest", "ModelScenarioTestGenerationResult"]
