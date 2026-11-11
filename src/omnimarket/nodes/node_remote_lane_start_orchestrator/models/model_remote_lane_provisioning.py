# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provisioning plan of a lane start and the record of each step (OMN-23528).

The remote-lane runner provisions a host around the engine: it resolves the registry root, writes the
runtime config, ships the plugin trees, makes the run directories, stages the ledger snapshot and the
run's files and mirrors the CLAIM before the engine; after the engine it stops the guard, drains its
spool, relays the reports, reads the queued rows and the lane's changes and closes out the worktrees,
and after the closing row it holds and retains the run directory. Which steps run, in which order, in
which phase, and which of them end the start when they fail, are VALUES: the overlay's
``remote_lane_start.provisioning`` section carries them and this model refuses a plan the transport
cannot carry out (the CLAIM mirror without the ledger snapshot and the staged CLAIM row, a fatal step
after the engine ran, a step named twice).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
    EnumLaneHostOperation,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: The staged file the transport's ``mirror_ledger`` reads the CLAIM row from (``CLAIM_ROW_FILE`` in the ssh adapter).
CLAIM_ROW_STAGED_NAME = "claim-row.txt"


class EnumLaneProvisioningPhase(StrEnum):
    """When in the lane start a step runs; the plan lists the phases in this order."""

    BEFORE_ENGINE = "before_engine"
    BEFORE_CLOSE = "before_close"
    AFTER_CLOSE = "after_close"


_PHASE_ORDER = {
    EnumLaneProvisioningPhase.BEFORE_ENGINE: 0,
    EnumLaneProvisioningPhase.BEFORE_CLOSE: 1,
    EnumLaneProvisioningPhase.AFTER_CLOSE: 2,
}

#: The host calls a plan may list: every transport call that is neither an engine call nor the worktree prepare.
PROVISIONING_OPERATIONS = frozenset(
    {
        EnumLaneHostOperation.RESOLVE_REGISTRY_ROOT,
        EnumLaneHostOperation.PREPARE_RUN_DIRS,
        EnumLaneHostOperation.STAGE_LEDGER_SNAPSHOT,
        EnumLaneHostOperation.STAGE_FILE,
        EnumLaneHostOperation.SHIP_PLUGIN_TREES,
        EnumLaneHostOperation.MIRROR_LEDGER,
        EnumLaneHostOperation.WRITE_RUNTIME_CONFIG,
        EnumLaneHostOperation.DRAIN_GUARD_SPOOL,
        EnumLaneHostOperation.RELAY_REPORTS,
        EnumLaneHostOperation.READ_LEDGER_OUTBOX,
        EnumLaneHostOperation.READ_LANE_CHANGES,
        EnumLaneHostOperation.CLOSEOUT_WORKTREES,
        EnumLaneHostOperation.HOLD_RUN_DIR_GUARD,
        EnumLaneHostOperation.RETAIN_RUN_ARTIFACTS,
        EnumLaneHostOperation.STOP_LANE_GUARD,
    }
)


class ModelLaneProvisioningStep(BaseModel):
    """One provisioning step: which host call, when, whether its failure ends the start, and what it stages."""

    model_config = _FROZEN

    step: EnumLaneHostOperation
    phase: EnumLaneProvisioningPhase
    #: True when a failure of this step ends the start with a closing row and no engine; False when the failure is
    #: recorded in the receipt and the lane goes on. The runner's behaviour, step by step.
    fatal: bool
    timeout_seconds: float = Field(gt=0)
    #: True when the runner runs the step only for a lane whose engine was started.
    only_if_engine_started: bool = False
    #: For ``stage_file``: the file name inside the run's context directory, and what it holds.
    stage_as: str = Field(default="", pattern=r"^[^/\\]*$")
    stage_content: Literal["", "brief", "claim_row"] = ""

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.step not in PROVISIONING_OPERATIONS:
            raise ValueError(f"{self.step.value} is not a provisioning step")
        stages = self.step is EnumLaneHostOperation.STAGE_FILE
        if stages and not (self.stage_as and self.stage_content):
            raise ValueError(
                "stage_file names the file (stage_as) and its content (stage_content)"
            )
        if not stages and (self.stage_as or self.stage_content):
            raise ValueError(
                f"{self.step.value} stages no named file; stage_as and stage_content belong to stage_file"
            )
        if self.fatal and self.phase is not EnumLaneProvisioningPhase.BEFORE_ENGINE:
            raise ValueError(
                f"{self.step.value}: a failure after the engine started cannot end the start; only before_engine steps are fatal"
            )
        return self


class ModelLaneProvisioningPlan(BaseModel):
    """The ordered steps and the runtime config the transport writes (the overlay's ``provisioning`` section)."""

    model_config = _FROZEN

    steps: tuple[ModelLaneProvisioningStep, ...] = ()
    #: The text the ``write_runtime_config`` step installs on a host that has none, and its place under the registry root;
    #: "" leaves the step reporting that the node did not run it.
    runtime_config_text: str = ""
    runtime_config_target: str = ""

    @model_validator(mode="after")
    def _plan_is_carried_out_by_the_transport(self) -> Self:
        last = -1
        seen: set[EnumLaneHostOperation] = set()
        staged: list[tuple[str, str]] = []
        for index, entry in enumerate(self.steps):
            order = _PHASE_ORDER[entry.phase]
            if order < last:
                raise ValueError(
                    f"provisioning.steps[{index}] {entry.step.value}: phase {entry.phase.value} comes after a later phase; "
                    "list before_engine, then before_close, then after_close"
                )
            last = order
            if entry.step is not EnumLaneHostOperation.STAGE_FILE:
                if entry.step in seen:
                    raise ValueError(
                        f"provisioning.steps[{index}]: {entry.step.value} is listed twice"
                    )
                seen.add(entry.step)
            else:
                if (entry.stage_as, entry.stage_content) in staged:
                    raise ValueError(
                        f"provisioning.steps[{index}]: {entry.stage_as} is staged twice"
                    )
                staged.append((entry.stage_as, entry.stage_content))
            if entry.step is EnumLaneHostOperation.MIRROR_LEDGER:
                has_snapshot = EnumLaneHostOperation.STAGE_LEDGER_SNAPSHOT in seen
                has_claim_row = (CLAIM_ROW_STAGED_NAME, "claim_row") in staged
                if not (has_snapshot and has_claim_row):
                    raise ValueError(
                        f"provisioning.steps[{index}]: mirror_ledger needs stage_ledger_snapshot and a stage_file of the "
                        f"claim_row as {CLAIM_ROW_STAGED_NAME} before it"
                    )
        if bool(self.runtime_config_text) != bool(self.runtime_config_target):
            raise ValueError(
                "provisioning: runtime_config_text and runtime_config_target are both set or both empty"
            )
        return self


class ModelLaneProvisioningRecord(BaseModel):
    """What one provisioning step did, as the receipt keeps it."""

    model_config = _FROZEN

    step: str
    phase: EnumLaneProvisioningPhase
    fatal: bool
    #: ``ok``; ``failed``; ``not-run`` (the transport declares the step is not carried out by the node);
    #: ``skipped`` (the orchestrator had nothing to hand the step, or its condition did not hold).
    status: Literal["ok", "failed", "not-run", "skipped"]
    detail: str = ""
    error: str | None = None
    cells: tuple[tuple[str, str], ...] = ()


__all__ = [
    "CLAIM_ROW_STAGED_NAME",
    "PROVISIONING_OPERATIONS",
    "EnumLaneProvisioningPhase",
    "ModelLaneProvisioningPlan",
    "ModelLaneProvisioningRecord",
    "ModelLaneProvisioningStep",
]
