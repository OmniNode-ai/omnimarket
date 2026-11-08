# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory ProtocolLaneHostTransport: answers from a script and logs every call in order (OMN-22848).

The default transport of the start effect. Each call appends one line to ``calls``, a list the
caller may share with its ledger fixture so one ordered log holds host commands and ledger
appends together. Each provisioning step is answered ok and logged, or refused with ``provision_error``. The engine is never run: ``exit_polls`` running answers come back from
``read_engine_exit`` before ``exit_code``, and ``read_log_tail`` hands out ``log_lines``.
"""

from __future__ import annotations

from typing import ClassVar, Literal

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
from pydantic import BaseModel, ConfigDict, Field

#: The calls that start an engine on a host; the CLAIM must precede the first of them.
ENGINE_START_CALLS = ("prepare_worktree", "start_engine_detached")


class ModelScriptedTransportScript(BaseModel):
    """How the scripted host answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prepare_error: str | None = None
    provision_error: str | None = None
    start_state: Literal["started", "failed", "unresolved"] = "started"
    start_error: str = "engine did not start"
    exit_polls: int = Field(default=0, ge=0)
    exit_code: int = 0
    log_lines: tuple[str, ...] = ()
    stop_confirmed: bool = True
    stop_exit_code: int | None = 143


class ScriptedLaneHostTransport:
    """Answer each host call from the script; record it in ``calls``."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self, script: ModelScriptedTransportScript, calls: list[str] | None = None
    ) -> None:
        self._script = script
        self.calls: list[str] = calls if calls is not None else []
        self._polls = 0
        self._stopped = False

    def prepare_worktree(
        self, request: ModelLanePrepareWorktreeRequest
    ) -> ModelLanePrepareWorktreeResult:
        self.calls.append(
            f"host:prepare_worktree host={request.host.name} repo={request.repo} sha={request.sha}"
        )
        if self._script.prepare_error:
            return ModelLanePrepareWorktreeResult(
                prepared=False, error=self._script.prepare_error
            )
        root = (request.registry_root_candidates or ("/registry",))[0]
        return ModelLanePrepareWorktreeResult(
            prepared=True,
            registry_root=root,
            workdir=f"{root}/runs/{request.run_id}/{request.repo}",
        )

    def start_engine_detached(
        self, request: ModelLaneStartEngineRequest
    ) -> ModelLaneStartEngineResult:
        self.calls.append(
            f"host:start_engine_detached host={request.host.name} argv0={request.engine_argv[0]}"
        )
        state = self._script.start_state
        if state == "started":
            return ModelLaneStartEngineResult(
                state="started", session_id=f"s-{request.run_id}"
            )
        if state == "failed":
            return ModelLaneStartEngineResult(
                state="failed", error=self._script.start_error
            )
        return ModelLaneStartEngineResult(state="unresolved")

    def read_engine_exit(
        self, request: ModelLaneReadEngineExitRequest
    ) -> ModelLaneReadEngineExitResult:
        self.calls.append(f"host:read_engine_exit host={request.host.name}")
        if self._stopped:
            return ModelLaneReadEngineExitResult(
                running=False, exit_code=self._script.stop_exit_code or 143
            )
        if self._polls < self._script.exit_polls:
            self._polls += 1
            return ModelLaneReadEngineExitResult(running=True)
        return ModelLaneReadEngineExitResult(
            running=False, exit_code=self._script.exit_code
        )

    def stop_engine(
        self, request: ModelLaneStopEngineRequest
    ) -> ModelLaneStopEngineResult:
        self.calls.append(f"host:stop_engine host={request.host.name}")
        self._stopped = self._script.stop_confirmed
        return ModelLaneStopEngineResult(
            confirmed=self._script.stop_confirmed,
            exit_code=self._script.stop_exit_code
            if self._script.stop_confirmed
            else None,
            detail="scripted stop",
        )

    def read_log_tail(
        self, request: ModelLaneReadLogTailRequest
    ) -> ModelLaneReadLogTailResult:
        self.calls.append(
            f"host:read_log_tail host={request.host.name} offset={request.offset}"
        )
        lines = self._script.log_lines[request.offset :]
        return ModelLaneReadLogTailResult(
            lines=lines, next_offset=request.offset + len(lines)
        )

    def _step(
        self, op: str, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        self.calls.append(f"host:{op} host={request.host.name} run={request.run_id}")
        if self._script.provision_error:
            return ModelLaneHostStepResult(ok=False, error=self._script.provision_error)
        return ModelLaneHostStepResult(ok=True, detail=f"scripted {op}")

    def resolve_registry_root(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneRegistryRootResult:
        self.calls.append(
            f"host:resolve_registry_root host={request.host.name} run={request.run_id}"
        )
        if self._script.provision_error:
            return ModelLaneRegistryRootResult(error=self._script.provision_error)
        return ModelLaneRegistryRootResult(registry_root="/registry")

    def prepare_run_dirs(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneRunDirsResult:
        self.calls.append(
            f"host:prepare_run_dirs host={request.host.name} run={request.run_id}"
        )
        if self._script.provision_error:
            return ModelLaneRunDirsResult(error=self._script.provision_error)
        root = request.registry_root or "/registry"
        return ModelLaneRunDirsResult(
            run_dir=f"{root}/runs/{request.run_id}",
            context_dir=f"{root}/runs/{request.run_id}.ctx",
        )

    def _stage(
        self, op: str, request: ModelLaneStageInputRequest
    ) -> ModelLaneStagedInput:
        self.calls.append(
            f"host:{op} host={request.host.name} run={request.run_id} name={request.name}"
        )
        if self._script.provision_error:
            return ModelLaneStagedInput(
                staged=False, error=self._script.provision_error
            )
        return ModelLaneStagedInput(
            staged=True, path=f"/registry/runs/{request.run_id}.ctx/{request.name}"
        )

    def stage_ledger_snapshot(
        self, request: ModelLaneStageInputRequest
    ) -> ModelLaneStagedInput:
        return self._stage("stage_ledger_snapshot", request)

    def stage_file(self, request: ModelLaneStageInputRequest) -> ModelLaneStagedInput:
        return self._stage("stage_file", request)

    def ship_plugin_trees(
        self, request: ModelLaneShipPluginTreesRequest
    ) -> ModelLaneShipPluginTreesResult:
        self.calls.append(
            f"host:ship_plugin_trees host={request.host.name} run={request.run_id} trees={len(request.trees)}"
        )
        if self._script.provision_error:
            return ModelLaneShipPluginTreesResult(
                shipped=False, error=self._script.provision_error
            )
        return ModelLaneShipPluginTreesResult(
            shipped=True,
            plugin_dirs=tuple(
                (t.name, f"/registry/plugins/{t.source_digest}") for t in request.trees
            ),
        )

    def mirror_ledger(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("mirror_ledger", request)

    def write_runtime_config(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("write_runtime_config", request)

    def drain_guard_spool(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("drain_guard_spool", request)

    def relay_reports(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("relay_reports", request)

    def read_ledger_outbox(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneLedgerOutboxRows:
        self.calls.append(
            f"host:read_ledger_outbox host={request.host.name} run={request.run_id}"
        )
        if self._script.provision_error:
            return ModelLaneLedgerOutboxRows(error=self._script.provision_error)
        return ModelLaneLedgerOutboxRows()

    def read_lane_changes(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneChangeList:
        self.calls.append(
            f"host:read_lane_changes host={request.host.name} run={request.run_id}"
        )
        if self._script.provision_error:
            return ModelLaneChangeList(error=self._script.provision_error)
        return ModelLaneChangeList()

    def closeout_worktrees(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneCloseoutCells:
        self.calls.append(
            f"host:closeout_worktrees host={request.host.name} run={request.run_id}"
        )
        if self._script.provision_error:
            return ModelLaneCloseoutCells(error=self._script.provision_error)
        return ModelLaneCloseoutCells()

    def hold_run_dir_guard(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("hold_run_dir_guard", request)

    def retain_run_artifacts(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("retain_run_artifacts", request)

    def stop_lane_guard(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return self._step("stop_lane_guard", request)


__all__ = [
    "ENGINE_START_CALLS",
    "ModelScriptedTransportScript",
    "ScriptedLaneHostTransport",
]
