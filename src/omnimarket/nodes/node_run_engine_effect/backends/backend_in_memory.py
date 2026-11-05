# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory run engine: the default backend, with the same observable behaviour and no durability.

Runs live in this process and die with it, which ``durable`` says. A step runs once per delivery; the in-memory engine
delivers each step once, so it cannot show the at-least-once hazard the durable backend has.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    decide_release,
)
from omnimarket.nodes.node_run_engine_effect.backends.step_runner import (
    ProtocolStepRunner,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumCancelOutcome,
    EnumReleaseOutcome,
    EnumRunStatus,
    EnumRunStepKind,
    EnumStartOutcome,
    ModelRunSpec,
    ModelRunState,
    ModelRunStepRecord,
)

_TERMINAL = (EnumRunStatus.COMPLETED, EnumRunStatus.FAILED, EnumRunStatus.CANCELLED)


@dataclass
class _Run:
    spec: ModelRunSpec
    status: EnumRunStatus = EnumRunStatus.RUNNING
    recorded: list[ModelRunStepRecord] = field(default_factory=list)
    waiting_on: str | None = None
    detail: str = ""
    releases: dict[str, str] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class BackendInMemory:
    """Holds runs in a dictionary and drives each with an asyncio task."""

    def __init__(self, runner: ProtocolStepRunner) -> None:
        self._runner = runner
        self._runs: dict[str, _Run] = {}

    @property
    def name(self) -> str:
        return "in_memory"

    @property
    def durable(self) -> bool:
        return False

    async def start_run(self, spec: ModelRunSpec) -> EnumStartOutcome:
        if spec.run_key in self._runs:
            return EnumStartOutcome.DUPLICATE
        run = _Run(spec=spec)
        self._runs[spec.run_key] = run
        run.task = asyncio.ensure_future(self._drive(run))
        return EnumStartOutcome.ACCEPTED

    async def release_run(
        self, run_key: str, condition: str, release_id: str
    ) -> EnumReleaseOutcome:
        state = await self.get_run_state(run_key)
        decided = decide_release(state, condition, release_id)
        if decided is not None:
            return decided
        run = self._runs[run_key]
        run.releases.setdefault(condition, release_id)
        run.wake.set()
        return EnumReleaseOutcome.ACCEPTED

    async def cancel_run(self, run_key: str) -> EnumCancelOutcome:
        run = self._runs.get(run_key)
        if run is None:
            return EnumCancelOutcome.UNKNOWN_RUN
        if run.status in _TERMINAL:
            return EnumCancelOutcome.DUPLICATE
        run.status = EnumRunStatus.CANCELLED
        run.waiting_on = None
        if run.task is not None:
            run.task.cancel()
        return EnumCancelOutcome.ACCEPTED

    async def get_run_state(self, run_key: str) -> ModelRunState:
        run = self._runs.get(run_key)
        if run is None:
            return ModelRunState(run_key=run_key, status=EnumRunStatus.NOT_FOUND)
        return ModelRunState(
            run_key=run_key,
            status=run.status,
            total_steps=len(run.spec.steps),
            recorded=tuple(run.recorded),
            waiting_on=run.waiting_on,
            detail=run.detail,
        )

    async def settle(self, run_key: str, timeout_seconds: float) -> ModelRunState:
        deadline = time.monotonic() + timeout_seconds
        state = await self.get_run_state(run_key)
        while time.monotonic() < deadline:
            if state.status in (*_TERMINAL, EnumRunStatus.WAITING):
                return state
            await asyncio.sleep(0.02)
            state = await self.get_run_state(run_key)
        return state

    async def health(self) -> bool:
        return True

    async def _drive(self, run: _Run) -> None:
        key = run.spec.run_key
        try:
            for step in run.spec.steps:
                if step.kind is EnumRunStepKind.WAIT_RELEASE:
                    condition = step.condition or ""
                    run.waiting_on = condition
                    run.status = EnumRunStatus.WAITING
                    while condition not in run.releases:
                        run.wake.clear()
                        await run.wake.wait()
                    run.waiting_on = None
                    run.status = EnumRunStatus.RUNNING
                    run.recorded.append(
                        ModelRunStepRecord(
                            name=step.name,
                            kind=step.kind,
                            condition=condition,
                            released_by=run.releases[condition],
                        )
                    )
                    continue
                result = await self._runner.run(step.argv, key, step.name, 1)
                if result.exit_code != 0:
                    run.status = EnumRunStatus.FAILED
                    run.detail = f"step {step.name} exited {result.exit_code}"
                    return
                run.recorded.append(
                    ModelRunStepRecord(
                        name=step.name, kind=step.kind, output=result.output
                    )
                )
            run.status = EnumRunStatus.COMPLETED
        except asyncio.CancelledError:
            run.status = EnumRunStatus.CANCELLED
            raise
