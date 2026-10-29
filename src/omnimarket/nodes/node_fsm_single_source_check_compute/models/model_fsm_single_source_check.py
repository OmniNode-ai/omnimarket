# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Texts in, issues out of node_fsm_single_source_check_compute (OMN-21477).

The node takes a reducer's contract text and the text of its Python sources; it reads no
tree, file, clock, environment or protocol.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSingleSourceIssue(StrEnum):
    CONTRACT_UNREADABLE = "contract_unreadable"
    STATE_MACHINE_MISSING = "state_machine_missing"
    SOURCE_UNPARSEABLE = "source_unparseable"
    STATE_TABLE = "state_table"
    STATE_BRANCH_CHAIN = "state_branch_chain"
    EXECUTE_TRANSITION_MISSING = "execute_transition_missing"


class ModelSourceText(BaseModel):
    """One Python source of the reducer: a label for the issues and its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    text: str


class ModelFsmSingleSourceCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_text: str
    sources: tuple[ModelSourceText, ...]


class ModelSingleSourceIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumSingleSourceIssue
    source: str
    line: int | None = None
    message: str = Field(min_length=1)


class ModelFsmSingleSourceCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str = "check_fsm_single_source"
    issues: tuple[ModelSingleSourceIssue, ...]
    exit_code: int
    state_names: tuple[str, ...]
