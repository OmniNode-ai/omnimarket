# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied lane rules text and checkout facts, and the resulting decision."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLaneRulesBlockRequest(BaseModel):
    """Render supplied text or resolve a block path without performing I/O.

    For block_path, supply either the resolved checkout path or the resolver's
    error text. block_exists is the caller's is_file result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["render_block", "contract_step", "contract_block", "block_path"]
    block_text: str = Field(
        default="", description="The block text read by the caller."
    )
    ticket: str = Field(default="", description="Ticket text, interpolated verbatim.")
    checkout_path: str | None = Field(
        default=None, description="Resolved checkout path."
    )
    resolve_error: str | None = Field(default=None, description="Resolver error text.")
    block_exists: bool = Field(
        default=True, description="Caller reports block.is_file()."
    )

    @model_validator(mode="after")
    def require_checkout_fact(self) -> Self:
        if (
            self.operation == "block_path"
            and self.checkout_path is None
            and self.resolve_error is None
        ):
            raise ValueError("block_path requires checkout_path or resolve_error")
        return self


class ModelLaneRulesBlockResult(BaseModel):
    """Text or path to print, or an error message to print to stderr."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["render_block", "contract_step", "contract_block", "block_path"]
    text: str | None = None
    path: str | None = None
    exit_code: Literal[0, 2] = 0
    message: str | None = None
