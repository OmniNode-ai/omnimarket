# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the ledger row grammar node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerRowGrammarRequest(BaseModel):
    """One question put to the ledger row grammar.

    ``row_refusals`` judges one row's shape (``line``). ``payload_refusal`` judges every row a
    payload opens, for the ledger named ``ledger_name``. ``state_refusal`` judges the payload
    against the ledger text already written (``existing``), for the rules that need it (ids, holds,
    leases, the worktree a TERMINAL closes). ``grammar`` prints the grammar as JSON.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["row_refusals", "payload_refusal", "state_refusal", "grammar"]
    line: str = Field(default="", description="The row, for row_refusals.")
    payload: str = Field(
        default="",
        description="The rows to append, for payload_refusal and state_refusal.",
    )
    existing: str = Field(
        default="", description="The ledger text already written, for state_refusal."
    )
    ledger_name: str = Field(
        default="",
        description="The ledger's file name; only a governed name is judged.",
    )


class ModelLedgerRowGrammarResult(BaseModel):
    """What the grammar says: every refusal of a row, the first refusal of a payload, or the grammar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["row_refusals", "payload_refusal", "state_refusal", "grammar"]
    refusals: list[str] = Field(
        default_factory=list,
        description="row_refusals: every reason, in grammar order.",
    )
    refusal: str | None = Field(
        default=None,
        description="payload_refusal and state_refusal: the first reason, or None.",
    )
    admitted: bool = Field(
        description="Whether the row or payload is admitted (no refusal)."
    )
    grammar_version: str = Field(description="The grammar's version string.")
    grammar_json: str | None = Field(
        default=None, description="grammar: the grammar as JSON."
    )
