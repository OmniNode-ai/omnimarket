# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the ticket contract render and parse."""

from __future__ import annotations

import re
from enum import StrEnum

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumTicketContractRenderOperation(StrEnum):
    """Which direction the request goes."""

    RENDER = "render"
    PARSE = "parse"


class EnumTicketContractRenderOutcome(StrEnum):
    """What the node did with the request."""

    RENDERED = "rendered"
    PARSED = "parsed"
    REFUSED = "refused"


class EnumTicketContractRenderFinding(StrEnum):
    """Why a body was refused or reported as drifted. Each names a thing to fix, never a retry."""

    RENDERED_TEXT_DRIFT = "rendered_text_drift"
    CONTRACT_NOT_SYNCED = "contract_not_synced"
    CONTRACT_BLOCK_MISSING = "contract_block_missing"
    CONTRACT_BLOCK_DUPLICATED = "contract_block_duplicated"
    CONTRACT_BLOCK_UNPARSEABLE = "contract_block_unparseable"
    CONTRACT_INVALID = "contract_invalid"


class ModelTicketContractRenderNormalization(BaseModel):
    """One regular-expression rewrite applied to both texts before they are compared.

    The list comes from an overlay: it names the rewrites the tracker's own markdown round trip
    makes, so a body the tracker re-wrapped is not reported as drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: str = Field(min_length=1, description="A regular expression.")
    replacement: str = Field(default="", description="Its replacement text.")

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"not a regular expression: {exc}") from exc
        return value


class ModelTicketContractRenderRequest(BaseModel):
    """Render a contract into a ticket body, or parse a body back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: EnumTicketContractRenderOperation
    contract: ModelTicketContract | None = Field(
        default=None, description="The contract to render; render only."
    )
    filing_lines: tuple[str, ...] = Field(
        default=(),
        description=(
            "The lane stamp and correlation stamp, from the caller; render only. They are "
            "not contract content and are never compared."
        ),
    )
    body: str = Field(default="", description="The ticket body to read; parse only.")
    normalizations: tuple[ModelTicketContractRenderNormalization, ...] = Field(
        default=(),
        description="The overlay's rewrites applied before the text comparison.",
    )

    @model_validator(mode="after")
    def _operation_has_its_input(self) -> ModelTicketContractRenderRequest:
        if (
            self.operation is EnumTicketContractRenderOperation.RENDER
            and self.contract is None
        ):
            raise ValueError("a render request carries a contract")
        if (
            self.operation is EnumTicketContractRenderOperation.PARSE
            and not self.body.strip()
        ):
            raise ValueError("a parse request carries a body")
        return self


class ModelTicketContractRenderFinding(BaseModel):
    """One finding, by code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: EnumTicketContractRenderFinding
    detail: str = ""


class ModelTicketContractRenderResult(BaseModel):
    """The body (render), or the filing lines and contract (parse), with every finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: EnumTicketContractRenderOutcome
    body: str = ""
    filing_lines: tuple[str, ...] = ()
    contract: ModelTicketContract | None = None
    block_fingerprint: str = Field(
        default="",
        description="The fingerprint the block states; empty when none was read.",
    )
    recomputed_fingerprint: str = Field(
        default="", description="The fingerprint recomputed from the block's contract."
    )
    findings: tuple[ModelTicketContractRenderFinding, ...] = ()
