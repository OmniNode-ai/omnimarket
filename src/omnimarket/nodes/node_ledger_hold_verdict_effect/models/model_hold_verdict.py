# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, verdict, event and overlay of node_ledger_hold_verdict_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    ModelReaderResult,
)

_LANE = r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$"


class EnumHoldVerdict(StrEnum):
    """HELD: a hold in force applies. CLEAR: a read answered and none applies. UNDECIDED: no read answered."""

    HELD = "HELD"
    CLEAR = "CLEAR"
    UNDECIDED = "UNDECIDED"


class EnumHoldSource(StrEnum):
    """Where the holds lines came from: the bus-fed node, the file (logged), or nowhere."""

    NODE = "node"
    FILE_FALLBACK = "file-fallback"
    NONE = "none"


class ModelHoldVerdictOverlay(BaseModel):
    """Every deployment fact of the node; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # True until cutover plan step L15; from L15 a failed node read is UNDECIDED, no file read.
    file_fallback_allowed: bool
    # The environment variable naming the file the node's events are journalled to.
    journal_env: str = Field(min_length=1)


class ModelHoldVerdictRequest(BaseModel):
    """One pull request, the reader's answer for ``holds``, and the file's lines when carried."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(pattern=r"^[A-Za-z0-9_.-]+#[0-9]+$")
    lane: str | None = Field(default=None, pattern=_LANE)
    node_answer: ModelReaderResult
    file_fallback_lines: tuple[str, ...] | None = None


class ModelHoldVerdictEvent(BaseModel):
    """The golden (``answered``) or error (``refused``) event of one verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReaderEventKind
    topic: str
    pr: str
    verdict: EnumHoldVerdict
    source: EnumHoldSource
    reason: str
    at: AwareDatetime
    hold_ids: tuple[str, ...] = ()


class ModelHoldVerdict(BaseModel):
    """The verdict a pause check prints; CLEAR never follows a failed read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    verdict: EnumHoldVerdict
    source: EnumHoldSource
    reason: str
    hold_ids: tuple[str, ...] = ()
    event: ModelHoldVerdictEvent | None = None

    def log_line(self) -> str:
        """One machine-readable line; a file answer carries ``source=file-fallback``."""
        holds = ",".join(self.hold_ids) or "none"
        return (
            f"HOLD-VERDICT pr={self.pr} verdict={self.verdict.value} "
            f"source={self.source.value} reason={self.reason} holds={holds}"
        )
