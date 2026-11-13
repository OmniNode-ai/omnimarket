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
    ModelLaneChangeList,
    ModelLaneCloseoutCells,
    ModelLaneHostStepRequest,
    ModelLaneHostStepResult,
    ModelLaneLedgerOutboxRows,
    ModelLanePrepareWorktreeRequest,
    ModelLanePrepareWorktreeResult,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadEngineExitResult,
    ModelLaneReadLogTailRequest,
    ModelLaneReadLogTailResult,
    ModelLaneRegistryRootResult,
    ModelLaneRunDirsResult,
    ModelLaneShipPluginTreesRequest,
    ModelLaneShipPluginTreesResult,
    ModelLaneStagedInput,
    ModelLaneStageInputRequest,
    ModelLaneStartEngineRequest,
    ModelLaneStartEngineResult,
    ModelLaneStopEngineRequest,
    ModelLaneStopEngineResult,
)
from pydantic import BaseModel, ConfigDict, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumLaneHostOperation(StrEnum):
    """Every call a lane start makes to its host, one member per transport method."""

    PREPARE_WORKTREE = "prepare_worktree"
    START_ENGINE_DETACHED = "start_engine_detached"
    READ_ENGINE_EXIT = "read_engine_exit"
    STOP_ENGINE = "stop_engine"
    READ_LOG_TAIL = "read_log_tail"
    RESOLVE_REGISTRY_ROOT = "resolve_registry_root"
    PREPARE_RUN_DIRS = "prepare_run_dirs"
    STAGE_LEDGER_SNAPSHOT = "stage_ledger_snapshot"
    STAGE_FILE = "stage_file"
    SHIP_PLUGIN_TREES = "ship_plugin_trees"
    MIRROR_LEDGER = "mirror_ledger"
    WRITE_RUNTIME_CONFIG = "write_runtime_config"
    DRAIN_GUARD_SPOOL = "drain_guard_spool"
    RELAY_REPORTS = "relay_reports"
    READ_LEDGER_OUTBOX = "read_ledger_outbox"
    READ_LANE_CHANGES = "read_lane_changes"
    CLOSEOUT_WORKTREES = "closeout_worktrees"
    HOLD_RUN_DIR_GUARD = "hold_run_dir_guard"
    RETAIN_RUN_ARTIFACTS = "retain_run_artifacts"
    STOP_LANE_GUARD = "stop_lane_guard"


REQUEST_FIELD: dict[EnumLaneHostOperation, str] = {
    EnumLaneHostOperation.PREPARE_WORKTREE: "prepare",
    EnumLaneHostOperation.START_ENGINE_DETACHED: "start",
    EnumLaneHostOperation.READ_ENGINE_EXIT: "engine_exit",
    EnumLaneHostOperation.STOP_ENGINE: "stop",
    EnumLaneHostOperation.READ_LOG_TAIL: "log_tail",
    EnumLaneHostOperation.RESOLVE_REGISTRY_ROOT: "step",
    EnumLaneHostOperation.PREPARE_RUN_DIRS: "step",
    EnumLaneHostOperation.STAGE_LEDGER_SNAPSHOT: "stage",
    EnumLaneHostOperation.STAGE_FILE: "stage",
    EnumLaneHostOperation.SHIP_PLUGIN_TREES: "ship",
    EnumLaneHostOperation.MIRROR_LEDGER: "step",
    EnumLaneHostOperation.WRITE_RUNTIME_CONFIG: "step",
    EnumLaneHostOperation.DRAIN_GUARD_SPOOL: "step",
    EnumLaneHostOperation.RELAY_REPORTS: "step",
    EnumLaneHostOperation.READ_LEDGER_OUTBOX: "step",
    EnumLaneHostOperation.READ_LANE_CHANGES: "step",
    EnumLaneHostOperation.CLOSEOUT_WORKTREES: "step",
    EnumLaneHostOperation.HOLD_RUN_DIR_GUARD: "step",
    EnumLaneHostOperation.RETAIN_RUN_ARTIFACTS: "step",
    EnumLaneHostOperation.STOP_LANE_GUARD: "step",
}

RESULT_FIELD: dict[EnumLaneHostOperation, str] = {
    EnumLaneHostOperation.PREPARE_WORKTREE: "prepare",
    EnumLaneHostOperation.START_ENGINE_DETACHED: "start",
    EnumLaneHostOperation.READ_ENGINE_EXIT: "engine_exit",
    EnumLaneHostOperation.STOP_ENGINE: "stop",
    EnumLaneHostOperation.READ_LOG_TAIL: "log_tail",
    EnumLaneHostOperation.RESOLVE_REGISTRY_ROOT: "registry_root",
    EnumLaneHostOperation.PREPARE_RUN_DIRS: "run_dirs",
    EnumLaneHostOperation.STAGE_LEDGER_SNAPSHOT: "staged",
    EnumLaneHostOperation.STAGE_FILE: "staged",
    EnumLaneHostOperation.SHIP_PLUGIN_TREES: "shipped",
    EnumLaneHostOperation.MIRROR_LEDGER: "step_result",
    EnumLaneHostOperation.WRITE_RUNTIME_CONFIG: "step_result",
    EnumLaneHostOperation.DRAIN_GUARD_SPOOL: "step_result",
    EnumLaneHostOperation.RELAY_REPORTS: "step_result",
    EnumLaneHostOperation.READ_LEDGER_OUTBOX: "outbox",
    EnumLaneHostOperation.READ_LANE_CHANGES: "changes",
    EnumLaneHostOperation.CLOSEOUT_WORKTREES: "closeout",
    EnumLaneHostOperation.HOLD_RUN_DIR_GUARD: "step_result",
    EnumLaneHostOperation.RETAIN_RUN_ARTIFACTS: "step_result",
    EnumLaneHostOperation.STOP_LANE_GUARD: "step_result",
}


def _exactly_the_operation_field(
    model: BaseModel,
    field: dict[EnumLaneHostOperation, str],
    names: list[str],
    operation: EnumLaneHostOperation,
) -> None:
    present = [name for name in names if getattr(model, name) is not None]
    if present != [field[operation]]:
        raise ValueError(
            f"operation {operation.value} carries exactly the field {field[operation]}, got {present}"
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
    step: ModelLaneHostStepRequest | None = None
    stage: ModelLaneStageInputRequest | None = None
    ship: ModelLaneShipPluginTreesRequest | None = None

    @model_validator(mode="after")
    def _one_request(self) -> Self:
        _exactly_the_operation_field(
            self,
            REQUEST_FIELD,
            list(dict.fromkeys(REQUEST_FIELD.values())),
            self.operation,
        )
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
    registry_root: ModelLaneRegistryRootResult | None = None
    run_dirs: ModelLaneRunDirsResult | None = None
    staged: ModelLaneStagedInput | None = None
    shipped: ModelLaneShipPluginTreesResult | None = None
    outbox: ModelLaneLedgerOutboxRows | None = None
    changes: ModelLaneChangeList | None = None
    closeout: ModelLaneCloseoutCells | None = None
    step_result: ModelLaneHostStepResult | None = None

    @model_validator(mode="after")
    def _one_result(self) -> Self:
        _exactly_the_operation_field(
            self,
            RESULT_FIELD,
            list(dict.fromkeys(RESULT_FIELD.values())),
            self.operation,
        )
        return self


__all__ = [
    "REQUEST_FIELD",
    "RESULT_FIELD",
    "EnumLaneHostOperation",
    "ModelRemoteLaneHostCall",
    "ModelRemoteLaneHostCallResult",
]
