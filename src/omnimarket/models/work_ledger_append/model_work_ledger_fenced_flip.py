# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fenced flip of the ledger's authority between the file judge and the reducer (OMN-23154).

Rebuild plan 4.3 step 8 (L13): the serve stops appending to the file. The handover is fenced: the
append node answers new commands with a retry receipt, waits for every in-flight append, switches
authority and lifts the fence. The rollback is the same fence in the other direction and restarts the
file judge from the reducer's records; it never regenerates the file.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumLedgerAuthority(StrEnum):
    """Which judge answers an append."""

    FILE = "file"
    REDUCER = "reducer"


class ModelFencedFlipPolicy(BaseModel):
    """The ``fenced_flip`` section of the node contract: waits and wording, never code constants."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drain_wait_seconds: float = Field(gt=0)
    retry_message: str = Field(min_length=1)


class ModelLedgerFlipResult(BaseModel):
    """What one handover did: the authority now answering and the sequence it was fenced at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authority: EnumLedgerAuthority
    fence_seq: int = Field(ge=0)


class ModelFileJudgeRestartState(BaseModel):
    """The reducer's records the file judge is rebuilt from at a rollback (never the file's text)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fence_seq: int = Field(
        ge=0, description="The highest committed ledger_seq at the fence."
    )
    decided_request_ids: tuple[str, ...] = Field(
        description="Every req= the reducer decided, so a retried committed request answers duplicate."
    )


class ModelAuthorityDecision(BaseModel):
    """The reducer's answer to one command: its verdict, the sequences it gave and the reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: str = Field(pattern=r"^(accepted|duplicate|refused|already-present)$")
    ledger_seqs: tuple[int, ...] = ()
    reason: str = ""


__all__: list[str] = [
    "EnumLedgerAuthority",
    "ModelAuthorityDecision",
    "ModelFencedFlipPolicy",
    "ModelFileJudgeRestartState",
    "ModelLedgerFlipResult",
]
