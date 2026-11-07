# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One call to a lane host and its typed answer (OMN-22848).

The request and result of each call are the omnibase_spi wire models of
ProtocolLaneHostTransport; this envelope names which call it is so the effect
takes one request model and returns one result model.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from omnibase_spi.contracts.services import (
    ModelLanePrepareWorktreeRequest,
    ModelLanePrepareWorktreeResult,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadEngineExitResult,
    ModelLaneReadLogTailRequest,
    ModelLaneReadLogTailResult,
    ModelLaneStartEngineRequest,
    ModelLaneStartEngineResult,
    ModelLaneStopEngineRequest,
    ModelLaneStopEngineResult,
)
from pydantic import BaseModel, ConfigDict, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumLaneHostOperation(StrEnum):
    """The five calls a lane start makes to its host."""

    PREPARE_WORKTREE = "prepare_worktree"
    START_ENGINE_DETACHED = "start_engine_detached"
    READ_ENGINE_EXIT = "read_engine_exit"
    STOP_ENGINE = "stop_engine"
    READ_LOG_TAIL = "read_log_tail"


_FIELD = {
    EnumLaneHostOperation.PREPARE_WORKTREE: "prepare",
    EnumLaneHostOperation.START_ENGINE_DETACHED: "start",
    EnumLaneHostOperation.READ_ENGINE_EXIT: "engine_exit",
    EnumLaneHostOperation.STOP_ENGINE: "stop",
    EnumLaneHostOperation.READ_LOG_TAIL: "log_tail",
}


def _exactly_the_operation_field(
    model: BaseModel, operation: EnumLaneHostOperation
) -> None:
    present = [name for name in _FIELD.values() if getattr(model, name) is not None]
    if present != [_FIELD[operation]]:
        raise ValueError(
            f"operation {operation.value} carries exactly the field {_FIELD[operation]}, got {present}"
        )


class ModelRemoteLaneHostCall(BaseModel):
    """One host call: the operation and its spi request."""

    model_config = _FROZEN

    operation: EnumLaneHostOperation
    prepare: ModelLanePrepareWorktreeRequest | None = None
    start: ModelLaneStartEngineRequest | None = None
    engine_exit: ModelLaneReadEngineExitRequest | None = None
    stop: ModelLaneStopEngineRequest | None = None
    log_tail: ModelLaneReadLogTailRequest | None = None

    @model_validator(mode="after")
    def _one_request(self) -> Self:
        _exactly_the_operation_field(self, self.operation)
        return self


class ModelRemoteLaneHostCallResult(BaseModel):
    """The spi result of one host call."""

    model_config = _FROZEN

    operation: EnumLaneHostOperation
    prepare: ModelLanePrepareWorktreeResult | None = None
    start: ModelLaneStartEngineResult | None = None
    engine_exit: ModelLaneReadEngineExitResult | None = None
    stop: ModelLaneStopEngineResult | None = None
    log_tail: ModelLaneReadLogTailResult | None = None

    @model_validator(mode="after")
    def _one_result(self) -> Self:
        _exactly_the_operation_field(self, self.operation)
        return self


__all__ = [
    "EnumLaneHostOperation",
    "ModelRemoteLaneHostCall",
    "ModelRemoteLaneHostCallResult",
]
