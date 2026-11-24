# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What installing a core or spi pull request head into its downstream repositories showed.

The landing controller asks one question before it merges a pull request into a repository other repositories
install: does the head, installed into each downstream repository, still import and pass that repository's smoke?
These models carry the answer, per downstream repository and as one verdict, so the node that runs the install
and the landing decision that holds the merge share one shape. Only a ``pass`` lets the merge go on: a ``break``
names each failing module, symbol and the file and line that holds the import, and an ``unread`` (the install or
the smoke did not run to an answer, or imported nothing) holds the head exactly as a break does.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SHA_PATTERN = r"^[0-9a-f]{40}$"


class EnumDownstreamVerdict(StrEnum):
    """What the downstream check says about one head."""

    PASS = "pass"
    BREAK = "break"
    UNREAD = "unread"


class ModelDownstreamBreak(BaseModel):
    """One failing import a downstream repository shows with the head installed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    downstream: str
    module: str = ""
    symbol: str = ""
    at: str = Field(
        default="-", description="file:line of the importing line, or - when unknown"
    )
    error: str = ""


class ModelDownstreamTargetReading(BaseModel):
    """One downstream repository's reading with the head installed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    downstream: str
    verdict: EnumDownstreamVerdict
    reason: str = ""
    modules_tried: int = 0
    breaks: tuple[ModelDownstreamBreak, ...] = ()


class ModelDownstreamGateFact(BaseModel):
    """The downstream check of one head: the verdict, each repository's reading and the prose behind a hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    head_sha: str = Field(pattern=SHA_PATTERN)
    verdict: EnumDownstreamVerdict
    targets: tuple[ModelDownstreamTargetReading, ...] = ()
    reasons: tuple[str, ...] = ()

    def line(self) -> str:
        broken = sum(len(t.breaks) for t in self.targets)
        return (
            f"downstream_check={self.verdict.value} repo={self.repo} head={self.head_sha[:12]} "
            f"targets={len(self.targets)} breaks={broken}"
        )


class EnumDownstreamGateVerdict(StrEnum):
    """What a head's downstream check says to the landing decision."""

    PASS = "pass"
    BREAK = "break"
    UNREAD = "unread"
    OTHER_HEAD = "other_head"


class ModelDownstreamGateReading(BaseModel):
    """The landing decision's reading of a head's downstream check, with the prose behind a hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumDownstreamGateVerdict
    detail: str = ""


__all__ = [
    "EnumDownstreamGateVerdict",
    "EnumDownstreamVerdict",
    "ModelDownstreamBreak",
    "ModelDownstreamGateFact",
    "ModelDownstreamGateReading",
    "ModelDownstreamTargetReading",
]
