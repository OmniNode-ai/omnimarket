# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the J2 join (rebuild plan section 4.1, piece 2 to piece 3): a canary on each host
publishes one command, placement consumes the host's capacity advertisement and one correlated
result returns on the bus.

The settings come from the node's overlay; nothing here has a default.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

NONCE_PLACEHOLDER = "{nonce}"


class EnumJoinJ2Outcome(StrEnum):
    """How one host's J2 run ended. Only ``GOLDEN`` is a success."""

    GOLDEN = "golden"
    NO_ADVERTISEMENT = "no_advertisement"
    STALE_ADVERTISEMENT = "stale_advertisement"
    PLACEMENT_REFUSED = "placement_refused"
    NO_RESULT = "no_result"
    UNCORRELATED_RESULT = "uncorrelated_result"
    COMMAND_FAILED = "command_failed"


class ModelJoinJ2Settings(BaseModel):
    """Every setting of the J2 join."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The floor pieces the join runs between: the canary's piece and the seam's piece.
    canary_piece: int = Field(ge=1, le=18)
    seam_piece: int = Field(ge=1, le=18)
    #: The node names the two chains are emitted under.
    canary_node: str = Field(min_length=1)
    seam_node: str = Field(min_length=1)
    #: The unit the canary publishes: a command the pool host runs at a pushed commit.
    repo: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7)
    #: The command's words; the one word holding ``{nonce}`` is where the canary's nonce goes.
    argv: tuple[str, ...] = Field(min_length=1)
    #: The line prefix the command prints before the nonce, so the result can be tied to it.
    result_marker: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    command_timeout_seconds: int = Field(ge=1)
    #: How long past the command's own limit the caller waits for the terminal event.
    result_wait_slack_seconds: int = Field(ge=0)
    #: An advertisement older than this is stale: placement would not trust it either.
    advert_max_age_seconds: int = Field(ge=1)
    need_tools: tuple[str, ...] = ()

    @field_validator("argv")
    @classmethod
    def _one_nonce_word(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if sum(NONCE_PLACEHOLDER in word for word in value) != 1:
            raise ValueError(f"exactly one argv word holds {NONCE_PLACEHOLDER}")
        return value


class ModelJoinJ2HostResult(BaseModel):
    """What one host's J2 run read, typed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    correlation_id: str
    outcome: EnumJoinJ2Outcome
    failure_code: str = ""
    detail: str = ""
    #: The unit the canary sent; empty when nothing was sent.
    work_unit_id: str = ""
    #: The host that answered, as the receipt names it.
    receipt_host: str = ""
    advert_age_seconds: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is EnumJoinJ2Outcome.GOLDEN


__all__ = [
    "NONCE_PLACEHOLDER",
    "EnumJoinJ2Outcome",
    "ModelJoinJ2HostResult",
    "ModelJoinJ2Settings",
]
