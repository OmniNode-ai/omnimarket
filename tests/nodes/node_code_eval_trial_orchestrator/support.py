# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stub host and scorer that drive the reducer through a whole run (OMN-23627).

The reducer is pure, so a run is a loop: feed a message, read the emissions,
answer each unit placed and each score sent, feed those answers back. The stub
host answers a placed try with a completed receipt; the scorer is a callable
from the score request to a verdict.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models import (
    ModelCodeEvalArm,
    ModelCodeEvalCandidate,
    ModelCodeEvalRunNudge,
    ModelCodeEvalRunRequest,
    ModelCodeEvalRunRow,
    ModelCodeEvalSettings,
)
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    HandlerCodeEvalTrialReducer,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import (
    ModelCodeEvalReduceInput,
    ModelCodeEvalReduceOutput,
    ModelNudgeMessage,
    ModelResolvedTask,
    ModelRunRequestedMessage,
    ModelScoreAnsweredMessage,
    ModelScoreToSend,
    ModelUnitAnsweredMessage,
    ModelUnitToPlace,
)

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
HOSTS = ("h1", "h2", "h3", "h4")

Scorer = Callable[
    [ModelScoreToSend], tuple[EnumCodeScoreVerdict, EnumCodeScoreSource | None]
]


def settings(**overrides: int | tuple[int, ...]) -> ModelCodeEvalSettings:
    fields: dict[str, object] = {
        "attempt_budgets": (1, 3),
        "try_timeout_seconds": 600,
        "score_timeout_seconds": 300,
        "receipt_slack_seconds": 60,
        "nudge_interval_seconds": 30,
        "completion_bound_seconds": 1800,
    }
    fields.update(overrides)
    return ModelCodeEvalSettings.model_validate(fields)


def run_request(
    n_tasks: int = 2, n_arms: int = 2, publish_cells: bool = False
) -> ModelCodeEvalRunRequest:
    return ModelCodeEvalRunRequest(
        correlation_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        tenant_id="lab",
        harness_revision="rev-1",
        publish_cells=publish_cells,
        candidates=tuple(
            ModelCodeEvalCandidate(
                repository="OmniNode-ai/omnimarket",
                merge_commit=f"{i + 1:040x}",
                contract_id=f"OMN-{1000 + i}",
            )
            for i in range(n_tasks)
        ),
        arms=tuple(
            ModelCodeEvalArm(model=f"model-{i}", backend_id=f"backend-{i}")
            for i in range(n_arms)
        ),
    )


def run_message(
    n_tasks: int = 2,
    n_arms: int = 2,
    *,
    cfg: ModelCodeEvalSettings | None = None,
    excluded: tuple[str, ...] = (),
) -> ModelRunRequestedMessage:
    tasks = tuple(
        ModelResolvedTask(task_key=f"task-{i}", band="S") for i in range(n_tasks)
    ) + tuple(
        ModelResolvedTask(task_key=f"excluded-{i}", excluded_reason=reason)
        for i, reason in enumerate(excluded)
    )
    return ModelRunRequestedMessage(
        request=run_request(n_tasks, n_arms),
        tasks=tasks,
        settings=cfg or settings(),
        digest="digest-1",
    )


def pass_controls_only(
    score: ModelScoreToSend,
) -> tuple[EnumCodeScoreVerdict, EnumCodeScoreSource | None]:
    if not score.model_arm:
        return EnumCodeScoreVerdict.PASS, None
    return EnumCodeScoreVerdict.FAIL_CHECK, None


def accepted_unit(
    unit: ModelUnitToPlace, **overrides: object
) -> ModelUnitAnsweredMessage:
    fields: dict[str, object] = {
        "unit_id": unit.unit_id,
        "status": "completed",
        "exit_code": 0,
        "diff": f"diff for {unit.task_key} {unit.model} {unit.try_ordinal}",
        "loop_status": "accepted",
        "duration_s": 10.0,
    }
    fields.update(overrides)
    return ModelUnitAnsweredMessage.model_validate(fields)


@dataclass
class Run:
    """The reducer, the row it holds and everything it emitted."""

    reducer: HandlerCodeEvalTrialReducer = field(
        default_factory=HandlerCodeEvalTrialReducer
    )
    row: ModelCodeEvalRunRow | None = None
    now: datetime = T0
    hosts: tuple[str, ...] = HOSTS
    outputs: list[ModelCodeEvalReduceOutput] = field(default_factory=list)

    def feed(self, message: object) -> ModelCodeEvalReduceOutput:
        out = self.reducer.handle(
            ModelCodeEvalReduceInput(
                row=self.row,
                message=message,
                now=self.now,
                free_hosts=self.hosts,  # type: ignore[arg-type]
            )
        )
        self.row = out.row
        self.outputs.append(out)
        return out

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def nudge(self) -> ModelCodeEvalReduceOutput:
        assert self.row is not None
        return self.feed(
            ModelNudgeMessage(
                nudge=ModelCodeEvalRunNudge(
                    correlation_id=self.row.run_id,
                    now=self.now,
                    tick_id=f"tick-{len(self.outputs)}",
                )
            )
        )

    def drive(
        self,
        first: ModelCodeEvalReduceOutput,
        scorer: Scorer = pass_controls_only,
        *,
        unit: Callable[[ModelUnitToPlace], object] = accepted_unit,
        redeliver: bool = False,
        max_steps: int = 500,
    ) -> None:
        """Answer every unit placed and score sent until nothing is left to answer."""
        todo: list[object] = []

        def take(out: ModelCodeEvalReduceOutput) -> None:
            for placed in out.unit_requests:
                todo.append(unit(placed))
            for sent in out.score_requests:
                verdict, source = scorer(sent)
                todo.append(
                    ModelScoreAnsweredMessage(
                        task_key=sent.task_key,
                        model_arm=sent.model_arm,
                        try_ordinal=sent.try_ordinal,
                        verdict=verdict,
                        source=source,
                    )
                )

        take(first)
        steps = 0
        while todo and steps < max_steps:
            steps += 1
            message = todo.pop(0)
            take(self.feed(message))
            if redeliver:
                self.feed(
                    message
                )  # at-least-once: the same answer again changes nothing

    @property
    def verdict_rows(self) -> list:
        return [r for o in self.outputs for r in o.verdict_rows]

    @property
    def score_requests(self) -> list:
        return [s for o in self.outputs for s in o.score_requests]

    @property
    def closes(self) -> list:
        return [o.closed for o in self.outputs if o.closed is not None]


def start(
    n_tasks: int = 2, n_arms: int = 2, **kwargs: object
) -> tuple[Run, ModelCodeEvalReduceOutput]:
    run = Run()
    return run, run.feed(run_message(n_tasks, n_arms, **kwargs))  # type: ignore[arg-type]
