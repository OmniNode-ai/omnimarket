# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of generating a node's golden and error chain tests (OMN-19998)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.nodes.node_event_chain_generator_compute.models.model_generated_chain_case import (
    ModelGeneratedChainCase,
)


class ModelScenarioTestGenerationRequest(BaseModel):
    """A node's whole contract document, read as data, and where its test imports from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(..., min_length=1)
    contract_document: dict[str, JsonValue]
    kept_receipts: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "The kept mutation receipts beside the contract. When given, the node also "
            "refuses a declaration whose named break stayed green (generated_test_cannot_fail)."
        ),
    )
    generator_version: str = Field(default="1.0.0", min_length=1)


class ModelScenarioTestGenerationResult(BaseModel):
    """The generated test module and every case with the break it must catch.

    A declaration the validator would refuse never yields a result: the handler raises
    ChainScenarioDeclarationError instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"]
    generated_cases: tuple[ModelGeneratedChainCase, ...] = ()
    case_names: tuple[str, ...] = ()
    break_pairs: int = 0
    test_source: str = ""
    test_hash: str = ""


__all__ = ["ModelScenarioTestGenerationRequest", "ModelScenarioTestGenerationResult"]
