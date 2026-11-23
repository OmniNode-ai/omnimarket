# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed answer of every command, and the stored answer ``lines`` prints."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_fill_orchestrator.models.enum_floor_fill_command import (
    EnumFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.enum_floor_fill_status import (
    EnumFloorFillStatus,
)


class ModelFloorFillKeyResult(BaseModel):
    """What one command did with one key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    kind: Literal["started", "failed", "mint-refused", "refused"]
    lane: str | None = None
    ticket: str | None = None
    cause: str | None = None


class ModelFloorFillAnswer(BaseModel):
    """``status``, the rendered lines, each key's result and the ``exit_code`` the caller exits with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: EnumFloorFillCommand
    status: EnumFloorFillStatus
    lines: tuple[str, ...] = ()
    key_results: tuple[ModelFloorFillKeyResult, ...] = ()
    exit_code: int = Field(ge=0)
    state_seq: int = Field(default=0, ge=0)
    observed_at: AwareDatetime | None = None

    def decision_projection(self) -> dict[str, object]:
        """What parity compares: the lines, the status, the exit code and each key's result."""
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "lines": list(self.lines),
            "key_results": [
                (r.key, r.kind, r.lane, r.ticket, r.cause) for r in self.key_results
            ],
        }


class ModelFloorFillStoredAnswer(BaseModel):
    """The second row of the table, key ``<scope>:answer``.

    ``seq`` rises by one per stored answer, so a reader that keeps its own history of the runs sees a gap in it.
    ``free_slots`` is the count of free slots this run's pool read saw, None when the read failed (a failed read is never
    a count of 0); ``lanes_started`` is the number of lanes this run started.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...]
    status: EnumFloorFillStatus
    observed_at: AwareDatetime
    seq: int = Field(ge=0)
    state_seq: int = Field(ge=0)
    free_slots: int | None = Field(default=None, ge=0)
    lanes_started: int = Field(default=0, ge=0)


__all__: list[str] = [
    "ModelFloorFillAnswer",
    "ModelFloorFillKeyResult",
    "ModelFloorFillStoredAnswer",
]
