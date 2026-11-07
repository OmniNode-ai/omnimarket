# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory ProtocolLaneHostTransport: answers from a script and logs every call in order (OMN-22848).

The default transport of the start effect. Each call appends one line to ``calls``, a list the
caller may share with its ledger fixture so one ordered log holds host commands and ledger
appends together. The engine is never run: ``exit_polls`` running answers come back from
``read_engine_exit`` before ``exit_code``, and ``read_log_tail`` hands out ``log_lines``.
"""

from __future__ import annotations

from typing import ClassVar, Literal

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
from pydantic import BaseModel, ConfigDict, Field

#: The calls that start an engine on a host; the CLAIM must precede the first of them.
ENGINE_START_CALLS = ("prepare_worktree", "start_engine_detached")


class ModelScriptedTransportScript(BaseModel):
    """How the scripted host answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prepare_error: str | None = None
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


__all__ = [
    "ENGINE_START_CALLS",
    "ModelScriptedTransportScript",
    "ScriptedLaneHostTransport",
]
