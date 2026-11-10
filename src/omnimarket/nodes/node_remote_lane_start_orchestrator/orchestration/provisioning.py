# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Issue the provisioning steps of a lane start to the host effect, in the overlay's order (OMN-23528).

Each step of the plan is one ``ModelRemoteLaneHostCall`` handed to the ssh transport effect; the answer is
read into a ``ModelLaneProvisioningRecord`` for the receipt. Which steps run, in which order and which of
them end the start when they fail come from ``ModelLaneProvisioningPlan``, never from this module. A step
the effect cannot carry out for want of an input (no plugin trees named, a ledger that cannot be read) is
recorded as failed or skipped without a host call; a step the transport declares it does not run is
``not-run``, and a fatal one of those ends the start like a failure.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneHostStepRequest,
    ModelLaneShipPluginTreesRequest,
    ModelLaneStageInputRequest,
)

from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
    RESULT_FIELD,
    EnumLaneHostOperation,
    ModelRemoteLaneHostCall,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    STEP_NOT_RUN_BY_NODE,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_provisioning import (
    EnumLaneProvisioningPhase,
    ModelLaneProvisioningPlan,
    ModelLaneProvisioningRecord,
    ModelLaneProvisioningStep,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartRequest,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.ledger_snapshot import (
    LedgerSnapshotError,
    build_ledger_snapshot,
)

#: What follows the step name in the transport's "not run" error; the orchestrator reads it as ``not-run``.
NOT_RUN_SUFFIX = STEP_NOT_RUN_BY_NODE.split("{op}", 1)[1]

#: The host-side failures a call may end in without a typed answer; each is recorded as a failed step.
_CALL_FAILURES = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


@dataclass
class ProvisioningState:
    """What the steps of one lane start have learned so far."""

    registry_root: str | None = None
    closeout_cells: tuple[str, ...] = ()
    records: list[ModelLaneProvisioningRecord] = field(default_factory=list)


def _read(
    op: EnumLaneHostOperation, answer: Any
) -> tuple[bool, str | None, tuple[tuple[str, str], ...], str]:
    """(ok, error, cells, detail) of one typed host answer."""
    if op is EnumLaneHostOperation.RESOLVE_REGISTRY_ROOT:
        cells = (
            (("registry_root", answer.registry_root),) if answer.registry_root else ()
        )
        return answer.registry_root is not None, answer.error, cells, ""
    if op is EnumLaneHostOperation.PREPARE_RUN_DIRS:
        ok = answer.run_dir is not None and answer.context_dir is not None
        return (
            ok,
            answer.error,
            (
                ("run_dir", answer.run_dir or ""),
                ("context_dir", answer.context_dir or ""),
            )
            if ok
            else (),
            "",
        )
    if op in (
        EnumLaneHostOperation.STAGE_LEDGER_SNAPSHOT,
        EnumLaneHostOperation.STAGE_FILE,
    ):
        return (
            answer.staged,
            answer.error,
            (("path", answer.path),) if answer.path else (),
            "",
        )
    if op is EnumLaneHostOperation.SHIP_PLUGIN_TREES:
        return answer.shipped, answer.error, tuple(answer.plugin_dirs), ""
    if op is EnumLaneHostOperation.READ_LEDGER_OUTBOX:
        return (
            answer.error is None,
            answer.error,
            (("rows", str(len(answer.rows))),),
            "",
        )
    if op is EnumLaneHostOperation.READ_LANE_CHANGES:
        return (
            answer.error is None,
            answer.error,
            (("changes", str(len(answer.changes))),),
            "",
        )
    if op is EnumLaneHostOperation.CLOSEOUT_WORKTREES:
        return (
            answer.error is None and not answer.unavailable,
            answer.error,
            tuple(("cell", c) for c in answer.cells),
            "the host has no closeout tool" if answer.unavailable else "",
        )
    return answer.ok, answer.error, tuple(answer.cells), answer.detail


class LaneProvisioner:
    """Run the plan's steps of one phase through the injected host effect."""

    def __init__(
        self,
        *,
        plan: ModelLaneProvisioningPlan,
        host: HandlerRemoteLaneStartEffect,
        echo: Callable[[str], None] = print,
    ) -> None:
        self._plan = plan
        self._host = host
        self._echo = echo

    def run_phase(
        self,
        phase: EnumLaneProvisioningPhase,
        state: ProvisioningState,
        *,
        request: ModelRemoteLaneStartRequest,
        host_ref: ModelLaneHostRef,
        claim_row: str,
        engine_started: bool,
    ) -> ModelLaneProvisioningRecord | None:
        """Run every step of ``phase`` in plan order; return the first fatal failure, else None.

        A fatal failure stops the phase: the steps after it do not run. A non-fatal failure is recorded and the
        phase goes on.
        """
        for entry in self._plan.steps:
            if entry.phase is not phase:
                continue
            record = self._one(
                entry, state, request, host_ref, claim_row, engine_started
            )
            state.records.append(record)
            self._say(request, host_ref, record)
            if entry.fatal and record.status in ("failed", "not-run"):
                return record
        return None

    # ------------------------------------------------------------------ one step

    def _one(
        self,
        entry: ModelLaneProvisioningStep,
        state: ProvisioningState,
        request: ModelRemoteLaneStartRequest,
        host_ref: ModelLaneHostRef,
        claim_row: str,
        engine_started: bool,
    ) -> ModelLaneProvisioningRecord:
        def record(status: str, **fields: Any) -> ModelLaneProvisioningRecord:
            return ModelLaneProvisioningRecord.model_validate(
                {
                    "step": entry.step.value,
                    "phase": entry.phase,
                    "fatal": entry.fatal,
                    "status": status,
                    **fields,
                }
            )

        if entry.only_if_engine_started and not engine_started:
            return record("skipped", detail="the engine was not started")
        try:
            call = self._call(entry, state, request, host_ref, claim_row)
        except LedgerSnapshotError as exc:
            return record("failed", error=str(exc))
        if call is None:
            return record(
                "skipped", detail="nothing to hand the step: no plugin trees were named"
            )
        try:
            result = self._host.handle(call)
        except _CALL_FAILURES as exc:
            return record("failed", error=f"{type(exc).__name__}: {exc}")
        ok, error, cells, detail = _read(
            entry.step, getattr(result, RESULT_FIELD[entry.step])
        )
        if entry.step is EnumLaneHostOperation.RESOLVE_REGISTRY_ROOT and ok:
            state.registry_root = dict(cells)["registry_root"]
        if entry.step is EnumLaneHostOperation.CLOSEOUT_WORKTREES and ok:
            state.closeout_cells = tuple(value for _, value in cells)
        if ok:
            return record("ok", cells=cells, detail=detail)
        if error and error.endswith(NOT_RUN_SUFFIX):
            return record("not-run", error=error, detail=detail)
        if not error and detail:  # a host with no closeout tool: the step did not run
            return record("not-run", detail=detail)
        return record(
            "failed", error=error or "the step failed without naming its error"
        )

    def _call(
        self,
        entry: ModelLaneProvisioningStep,
        state: ProvisioningState,
        request: ModelRemoteLaneStartRequest,
        host_ref: ModelLaneHostRef,
        claim_row: str,
    ) -> ModelRemoteLaneHostCall | None:
        op = entry.step
        common = {"host": host_ref, "lane": request.lane, "run_id": request.run_id}
        if op is EnumLaneHostOperation.STAGE_LEDGER_SNAPSHOT:
            ledger = Path(request.ledger_path)
            snapshot = build_ledger_snapshot(ledger)
            return ModelRemoteLaneHostCall(
                operation=op,
                stage=ModelLaneStageInputRequest(
                    **common,
                    name=ledger.name,
                    content=snapshot.content,
                    timeout_seconds=entry.timeout_seconds,
                ),
            )
        if op is EnumLaneHostOperation.STAGE_FILE:
            text = request.brief if entry.stage_content == "brief" else f"{claim_row}\n"
            return ModelRemoteLaneHostCall(
                operation=op,
                stage=ModelLaneStageInputRequest(
                    **common,
                    name=entry.stage_as,
                    content=text,
                    timeout_seconds=entry.timeout_seconds,
                ),
            )
        if op is EnumLaneHostOperation.SHIP_PLUGIN_TREES:
            if not request.plugin_trees:
                return None
            return ModelRemoteLaneHostCall(
                operation=op,
                ship=ModelLaneShipPluginTreesRequest(
                    **common,
                    trees=request.plugin_trees,
                    timeout_seconds=entry.timeout_seconds,
                ),
            )
        return ModelRemoteLaneHostCall(
            operation=op,
            step=ModelLaneHostStepRequest(
                **common,
                registry_root=state.registry_root,
                timeout_seconds=entry.timeout_seconds,
            ),
        )

    def _say(
        self,
        request: ModelRemoteLaneStartRequest,
        host_ref: ModelLaneHostRef,
        record: ModelLaneProvisioningRecord,
    ) -> None:
        why = record.error or record.detail
        self._echo(
            f"REMOTE-LANE-PROVISION lane={request.lane} host={host_ref.name} step={record.step} "
            f"phase={record.phase.value} status={record.status} fatal={str(record.fatal).lower()}"
            + (f": {why[:200]}" if why else "")
        )


__all__ = ["NOT_RUN_SUFFIX", "LaneProvisioner", "ProvisioningState"]
