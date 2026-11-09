# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed boundaries of the reducer judge: the shadow verdict and the guard replay (OMN-22915, L10 and L11).

The reducer judges a row with the ported guards of the ledger row grammar. In shadow it writes
nothing: it records its verdict beside the file judge's. The guard replay re-judges recorded rows
in order, each against the ledger as it stood before that row, and lists every row whose verdict
changed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumJudgeVerdict(StrEnum):
    ACCEPT = "accept"
    REFUSE = "refuse"


class ModelJudgeVerdict(BaseModel):
    """One judge's answer: accept, or refuse under a named rule with the full reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumJudgeVerdict
    rule: str | None = Field(
        default=None,
        description="The refusal rule's name (the text before the reason); None when accepted.",
    )
    reason: str | None = Field(
        default=None, description="The grammar's full refusal text; None when accepted."
    )


class ModelShadowJudgeRequest(BaseModel):
    """A row the live judge answered, the ledger text before it, and the live verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_ref: str = Field(
        min_length=1,
        description="The request id or row id the verdicts are filed under.",
    )
    row: str = Field(
        min_length=1, description="The row (or rows) the live judge was asked about."
    )
    existing: str = Field(
        description="The ledger text before this row: every row the live judge accepted earlier."
    )
    ledger_name: str = Field(min_length=1)
    live: ModelJudgeVerdict


class ModelShadowVerdictRecord(BaseModel):
    """The shadow verdict filed beside the live one, and the difference when they disagree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_ref: str
    live: ModelJudgeVerdict
    shadow: ModelJudgeVerdict
    agrees: bool
    difference: str | None = Field(
        default=None,
        description="Names how the two verdicts differ; None when they agree.",
    )


class ModelRecordedRow(BaseModel):
    """One recorded row with the verdict the live judge gave it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_ref: str = Field(min_length=1)
    row: str = Field(min_length=1)
    recorded: ModelJudgeVerdict
    at: AwareDatetime | None = None


class ModelGuardReplayRequest(BaseModel):
    """Recorded rows in order, replayed through the reducer's guards."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_name: str = Field(min_length=1)
    entries: tuple[ModelRecordedRow, ...]
    preceding_ledger: str = Field(
        default="",
        description="The ledger text the live judge had accepted before the first entry: the history "
        "before a recorded window, so open HOLDs, declared ids and CLAIM facts still count.",
    )


class ModelReplayDifference(BaseModel):
    """A row whose replayed verdict or refusal rule differs from the recorded one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=1)
    row_ref: str
    recorded: ModelJudgeVerdict
    replayed: ModelJudgeVerdict
    difference: str


class ModelGuardReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    judged: int = Field(ge=0)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    differences: tuple[ModelReplayDifference, ...] = ()
    equivalent: bool


__all__: list[str] = [
    "EnumJudgeVerdict",
    "ModelGuardReplayRequest",
    "ModelGuardReplayResult",
    "ModelJudgeVerdict",
    "ModelRecordedRow",
    "ModelReplayDifference",
    "ModelShadowJudgeRequest",
    "ModelShadowVerdictRecord",
]
