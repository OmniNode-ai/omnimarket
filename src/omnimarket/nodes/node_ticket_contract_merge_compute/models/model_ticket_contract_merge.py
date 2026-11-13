# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the ticket contract merge."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTicketContractMergeOutcome(StrEnum):
    """Whether the three texts merged."""

    MERGED = "merged"
    REFUSED = "refused"


class EnumTicketContractMergeRefusal(StrEnum):
    """Why a merge was refused. Every one leaves the conflict to a person or worker."""

    UNPARSEABLE = "unparseable"
    DUPLICATE_ID = "duplicate_id"
    DIVERGENT_ENTRY = "divergent_entry"
    DIVERGENT_FIELD = "divergent_field"
    REMOVED_AND_CHANGED = "removed_and_changed"
    INVALID_CONTRACT = "invalid_contract"
    RENDER_MISMATCH = "render_mismatch"


class ModelTicketContractMergeRequest(BaseModel):
    """Three contract texts. ``incoming`` is laid over ``current``, whose bytes it keeps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_text: str = Field(description="The common ancestor; empty when there is none.")
    current_text: str = Field(description="The side whose text the result keeps.")
    incoming_text: str = Field(description="The side whose changes are laid over it.")
    source: str = Field(default="contract", description="The path, for refusal text.")


class ModelTicketContractMergeRefusal(BaseModel):
    """One reason the merge was refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: EnumTicketContractMergeRefusal
    section: str = ""
    entry: str = ""
    detail: str = ""


class ModelTicketContractMergeResult(BaseModel):
    """The merged text, or every reason there is none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: EnumTicketContractMergeOutcome
    merged_text: str = ""
    entries_from_incoming: tuple[str, ...] = ()
    refusals: tuple[ModelTicketContractMergeRefusal, ...] = ()
