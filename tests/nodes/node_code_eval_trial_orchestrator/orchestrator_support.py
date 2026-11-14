# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory bus around the orchestrator handler: a stub host, a scorer and a stub projection (OMN-23627).

Exemplar: ``tests/nodes/node_code_eval_trial_orchestrator/support.py`` (the reducer's own
drive loop). Here the unit under test is the handler: every message it emits is routed as
the bus would route it. A unit goes to the stub host, which answers with a receipt; a
score request goes to the scorer, which answers with a scored event; a row goes to the
stub projection; the closed event is fed back to the closed route. Nothing waits.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from omnimarket.events.context_roi import ModelContextRoiRunResult
from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
from omnimarket.models.code_eval_trial.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_nudge import (
    ModelCodeEvalRunNudge,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalArm,
    ModelCodeEvalCandidate,
    ModelCodeEvalRunRequest,
)
from omnimarket.models.code_eval_trial.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
    task_key_for,
)
from omnimarket.models.enum_code_score import (
    EnumCodeScoreCheckOutcome,
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.models.eval_item.model_eval_item_traced import (
    ModelEvalItemTracePart,
)
from omnimarket.models.model_code_score import (
    ModelCodeScoreCheckFile,
    ModelCodeScoreCheckResult,
    ModelCodeScoreContractRef,
    ModelCodeScored,
    ModelCodeScoreOverlay,
    ModelCodeScoreRequest,
    ModelCodeScoreTimings,
    ModelCodeScoreVerdict,
)
from omnimarket.models.model_delegated_code_edit import (
    EnumCodeEditStatus,
    ModelCodeEditResult,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.handlers.handler_code_eval_trial_orchestrator import (
    HandlerCodeEvalTrialOrchestrator,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.free_hosts import (
    StaticFreeHosts,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.leg import (
    CodeEvalLegConfig,
    CodeEvalLegPorts,
)

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
HOSTS = ("h1", "h2", "h3", "h4")
SHA = "a" * 40
SHA256 = "b" * 64
REVISION = "sha256:rev-a"

Scorer = Callable[
    [ModelCodeScoreRequest], tuple[EnumCodeScoreVerdict, EnumCodeScoreSource | None]
]
Host = Callable[[ModelLabWorkUnitRequest], BaseModel | None]


def overlay(**overrides: object) -> ModelCodeEvalRunnerOverlay:
    fields: dict[str, object] = {
        "attempt_budgets": (1, 3),
        "try_timeout_seconds": 600,
        "receipt_slack_seconds": 60,
        "nudge_interval_seconds": 30,
        "max_turns": 8,
        "max_diff_bytes": 100_000,
        "draft_checks": ["uv run python -c pass"],
        "need_tools": ["uv"],
        "source_surface": "node_code_eval_trial_orchestrator",
        "unit_lane_prefix": "",
        "onex_executable": "onex",
        "caller_ticket": "OMN-23627",
    }
    fields.update(overrides)
    return ModelCodeEvalRunnerOverlay.model_validate(fields)


def score_overlay() -> ModelCodeScoreOverlay:
    return ModelCodeScoreOverlay(
        check_timeout_s=60,
        env_sync_timeout_s=60,
        runnable_check_types=("test_passes", "command"),
        env_sync_command="uv sync --frozen",
        scratch_root="/scratch/example",
        repo_roots={"example/repo": "/srv/example/repo"},
        protected_paths={"example/repo": ("contracts/",)},
    )


def leg_config(
    *, bound: int = 7200, score_timeout: int = 300, **overrides: object
) -> CodeEvalLegConfig:
    return CodeEvalLegConfig(
        overlay=overlay(**overrides),
        score_overlay=score_overlay(),
        completion_bound_seconds=bound,
        score_timeout_seconds=score_timeout,
    )


def candidates(n: int) -> tuple[ModelCodeEvalCandidate, ...]:
    return tuple(
        ModelCodeEvalCandidate(
            repository="example/repo",
            merge_commit=f"{i + 1:040x}",
            contract_id=f"OMN-{1000 + i}",
        )
        for i in range(n)
    )


def run_request(
    n_tasks: int = 2, n_arms: int = 2, *, publish_cells: bool = False
) -> ModelCodeEvalRunRequest:
    return ModelCodeEvalRunRequest(
        correlation_id=RUN_ID,
        tenant_id="lab",
        harness_revision="rev-1",
        publish_cells=publish_cells,
        candidates=candidates(n_tasks),
        arms=tuple(
            ModelCodeEvalArm(model=f"model-{i}", backend_id=f"backend-{i}")
            for i in range(n_arms)
        ),
    )


class InMemorySuiteReader:
    """Material for each candidate, the same for the same candidate; counts its reads."""

    def __init__(
        self, *, excluded: dict[str, str] | None = None, task_text: str = "fix it"
    ) -> None:
        self.reads: list[str] = []
        self._excluded = excluded or {}
        self._task_text = task_text

    def read(self, candidates: Iterable[Any]) -> tuple[ModelCodeEvalTaskMaterial, ...]:
        out = []
        for candidate in candidates:
            key = task_key_for(candidate)
            self.reads.append(key)
            held = f"held diff of {key}"
            out.append(
                ModelCodeEvalTaskMaterial(
                    task_key=key,
                    repository=candidate.repository,
                    base_commit=SHA,
                    task_text=f"{self._task_text} {key}",
                    writable_globs=("src/**",),
                    contract=ModelCodeScoreContractRef(
                        repository=candidate.repository,
                        path=f"contracts/{candidate.contract_id}.yaml",
                        revision=candidate.merge_commit,
                        sha256=SHA256,
                    ),
                    check_files=(
                        ModelCodeScoreCheckFile(
                            path="tests/test_x.py",
                            sha256=SHA256,
                            revision=candidate.merge_commit,
                        ),
                    ),
                    held_diff=held,
                    band="S",
                    material_hash=hashlib.sha256(held.encode()).hexdigest(),
                    excluded_reason=self._excluded.get(key),
                )
            )
        return tuple(out)


def pass_controls_only(
    request: ModelCodeScoreRequest,
) -> tuple[EnumCodeScoreVerdict, EnumCodeScoreSource | None]:
    if not request.try_identity.model_arm:
        return EnumCodeScoreVerdict.PASS, None
    return EnumCodeScoreVerdict.FAIL_CHECK, None


def completed_host(
    unit: ModelLabWorkUnitRequest, **overrides: object
) -> ModelLabWorkUnitReceipt:
    fields: dict[str, object] = {
        "work_unit_id": unit.work_unit_id,
        "target_host": unit.target_host,
        "host": unit.target_host,
        "repo": unit.repo,
        "commit_sha": unit.commit_sha,
        "lane": unit.lane,
        "kind": "code_draft",
        "status": EnumLabWorkUnitStatus.COMPLETED,
        "exit_code": 0,
        "duration_seconds": 10.0,
        "diff": f"diff of {unit.work_unit_id}",
        "code_edit_result": ModelCodeEditResult(
            loop_run_id="loop-1",
            status=EnumCodeEditStatus.ACCEPTED,
            turns=1,
            model_revision=REVISION,
        ),
    }
    fields.update(overrides)
    return ModelLabWorkUnitReceipt.model_validate(fields)


def scored_event(
    request: ModelCodeScoreRequest,
    verdict: EnumCodeScoreVerdict,
    source: EnumCodeScoreSource | None,
    check_results: tuple[ModelCodeScoreCheckResult, ...] = (),
) -> ModelCodeScored:
    return ModelCodeScored(
        correlation_id=request.correlation_id,
        repo=request.repo,
        base_commit=request.base_commit,
        draft_diff_sha256=hashlib.sha256(request.draft_diff.encode()).hexdigest(),
        try_identity=request.try_identity,
        gate_accepted=None,
        verdict=ModelCodeScoreVerdict(
            checks=verdict, source=source, check_results=check_results
        ),
        timings=ModelCodeScoreTimings(),
    )


@dataclass
class Bus:
    """The handler, a clock, a stub host, a scorer and a stub projection."""

    handler: HandlerCodeEvalTrialOrchestrator
    reader: InMemorySuiteReader
    scorer: Scorer = pass_controls_only
    host: Host = field(default_factory=lambda: completed_host)
    now: datetime = T0
    units: list[ModelLabWorkUnitRequest] = field(default_factory=list)
    scores: list[ModelCodeScoreRequest] = field(default_factory=list)
    projection: list[ModelContextRoiRunResult] = field(default_factory=list)
    closes: list[ModelCodeEvalRunClosed] = field(default_factory=list)
    nudges: list[ModelCodeEvalRunNudge] = field(default_factory=list)
    queue: list[BaseModel] = field(default_factory=list)
    held_back: list[BaseModel] = field(default_factory=list)
    #: A message kind whose answers are lost (never delivered).
    lose: tuple[type, ...] = ()
    #: The whole output of the one check a try's score reports (empty: no check result on the event).
    check_output: str = ""
    parts: list[ModelEvalItemTracePart] = field(default_factory=list)
    reader_candidates: tuple = ()

    def send(self, message: BaseModel) -> list[BaseModel]:
        emitted = asyncio.run(self.handler.handle(message))
        for event in emitted:
            self._route(event)
        return emitted

    def _route(self, event: BaseModel) -> None:
        if isinstance(event, ModelLabWorkUnitRequest):
            self.units.append(event)
            if ModelLabWorkUnitRequest in self.lose:
                return
            answer = self.host(event)
            if answer is not None:
                self.queue.append(answer)
        elif isinstance(event, ModelCodeScoreRequest):
            self.scores.append(event)
            if ModelCodeScoreRequest in self.lose:
                return
            verdict, source = self.scorer(event)
            results = (
                (
                    ModelCodeScoreCheckResult(
                        item_id="dod-1",
                        check_type="test_passes",
                        command="uv run pytest tests/test_x.py",
                        outcome=EnumCodeScoreCheckOutcome.FAILED,
                        exit_code=1,
                        output=self.check_output,
                    ),
                )
                if self.check_output and event.try_identity.model_arm
                else ()
            )
            self.queue.append(scored_event(event, verdict, source, results))
        elif isinstance(event, ModelEvalItemTracePart):
            self.parts.append(event)
        elif isinstance(event, ModelContextRoiRunResult):
            self.projection.append(event)
        elif isinstance(event, ModelCodeEvalRunClosed):
            self.closes.append(event)
            self.queue.append(event)
        elif isinstance(event, ModelCodeEvalRunNudge):
            self.nudges.append(event)

    def drain(self, *, redeliver: bool = False, max_steps: int = 500) -> None:
        steps = 0
        while self.queue and steps < max_steps:
            steps += 1
            message = self.queue.pop(0)
            self.send(message)
            if redeliver:
                self.send(message)

    def nudge(self) -> list[BaseModel]:
        return self.send(
            ModelCodeEvalRunNudge(
                correlation_id=RUN_ID, now=self.now, tick_id=f"t{len(self.nudges)}"
            )
        )

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    @property
    def rows(self) -> list:
        return [row for result in self.projection for row in result.rows]


def make_bus(
    *,
    reader: InMemorySuiteReader | None = None,
    hosts: tuple[str, ...] = HOSTS,
    config: CodeEvalLegConfig | None = None,
    **kwargs: Any,
) -> Bus:
    reader = reader or InMemorySuiteReader()
    holder: list[datetime] = [T0]
    handler = HandlerCodeEvalTrialOrchestrator(
        config=config or leg_config(),
        ports=CodeEvalLegPorts(suite_reader=reader, free_hosts=StaticFreeHosts(hosts)),
        clock=lambda: holder[0],
    )
    bus = Bus(handler=handler, reader=reader, **kwargs)

    class _Clock:
        def __call__(self) -> datetime:
            return bus.now

    handler._clock = _Clock()  # the bus owns the clock
    return bus


def unit_failure(unit: ModelLabWorkUnitRequest) -> ModelLabWorkCommandFailure:
    return ModelLabWorkCommandFailure(
        work_unit_id=unit.work_unit_id,
        host=unit.target_host,
        lane=unit.lane,
        correlation_id=str(RUN_ID),
        error_message="handler failed",
    )
